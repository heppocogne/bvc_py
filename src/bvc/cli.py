# 引数解析・表示整形・終了コード・--json。設計書 1.1節・3.7節。
# 情報・警告・エラーのメッセージは logging で出す(情報は stdout、警告以上は stderr)。
# コマンドの結果そのもの(log の表示、--json、--version)は stdout へ print する。

from __future__ import annotations

import argparse
import json
import logging
import sys
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from . import __version__
from .errors import BvcError, SafetyAbort, UsageError
from .model import (
    BranchInfo,
    Commit,
    CommitResult,
    DiscardResult,
    GcReport,
    LogEntry,
    MoveResult,
    VerifyReport,
    WorkState,
)
from .repo import Repo

# 成功
EXIT_OK: Final[int] = 0
# エラー(データ不整合、見つからない等)
EXIT_ERROR: Final[int] = 1
# 引数の誤り
EXIT_USAGE: Final[int] = 2
# 安全のため中止(ファイルの欠落、使用中・書き込み中、ロック中)
EXIT_ABORT: Final[int] = 3


# ---------------------------------------------------------------------------
# ログの出力先
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


class _Formatter(logging.Formatter):
    # 警告以上には接頭辞を付ける。record.prefix があればそれを使う(例: "中止")。
    _PREFIX: ClassVar[dict[int, str]] = {
        logging.WARNING: "警告",
        logging.ERROR: "エラー",
        logging.CRITICAL: "エラー",
    }

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        prefix = getattr(record, "prefix", None) or self._PREFIX.get(record.levelno)
        return f"{prefix}: {msg}" if prefix else msg


def setup_logging(quiet: bool = False) -> None:
    # bvc パッケージのロガーの出力先を設定する。呼び出すたびに作り直す(その時点の sys.stdout/stderr を使う)。
    root = logging.getLogger("bvc")
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    root.propagate = False
    fmt = _Formatter("%(message)s")
    if not quiet:
        out = logging.StreamHandler(sys.stdout)
        out.setLevel(logging.INFO)
        out.addFilter(lambda r: r.levelno < logging.WARNING)
        out.setFormatter(fmt)
        root.addHandler(out)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)
    root.addHandler(err)


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # argparse を構成する(M2-11)。
    parser = argparse.ArgumentParser(
        prog="bvc",
        description="大容量バイナリファイル向けのローカル専用バージョン管理ツール",
        add_help=False,
    )
    parser.add_argument(
        "-C",
        dest="workdir",
        metavar="<パス>",
        type=Path,
        default=None,
        help="作業フォルダを指定する(省略時はカレントから上位へ .bvc を探す)",
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出力する")
    parser.add_argument(
        "-q", dest="quiet", action="store_true", help="通常の出力を抑制する"
    )
    parser.add_argument("--help", action="store_true", help="ヘルプを表示")
    parser.add_argument("--version", action="store_true", help="バージョンを表示")

    sub = parser.add_subparsers(dest="command", metavar="<コマンド>", title="コマンド")

    p = sub.add_parser("init", help="リポジトリを作成する")
    p.add_argument(
        "path", nargs="?", default=None, help="作業フォルダ(省略時は -C またはカレント)"
    )
    p.add_argument(
        "--track",
        action="append",
        required=True,
        metavar="<パターン>",
        help="追跡パターン(複数指定可)",
    )
    p.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="<パターン>",
        help="除外パターン(複数指定可)",
    )

    p = sub.add_parser("commit", help="追跡ファイルの現状を版として記録する")
    p.add_argument("-m", "--message", default="", help="メッセージ")
    p.add_argument(
        "--allow-missing",
        action="store_true",
        help="見つからない追跡ファイルを削除として記録する",
    )
    p.add_argument(
        "--rename",
        action="append",
        dest="renames",
        metavar="<旧>=<新>",
        help="名前変更を手動指定する(複数指定可)",
    )

    p = sub.add_parser("log", help="版のツリーを表示する")
    p.add_argument("-n", dest="limit", type=int, metavar="<件数>", help="表示件数")
    p.add_argument("--discarded", action="store_true", help="削除済みの版も表示する")

    allow_missing_help = "見つからない追跡ファイルを、自動コミットで削除として記録する"
    for name, help_text in (("undo", "1つ前の版に戻る"), ("redo", "戻したのを取り消す(先端へ進む)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-m", "--message", dest="reason", default="", metavar="<理由>", help="理由(操作ログに記録する)")
        p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)
        p.add_argument(
            "--skip-broken", action="store_true", help="壊れた版を飛ばして、同じ方向で最も近い健全な版へ移動する"
        )

    p = sub.add_parser("goto", help="指定の版へ移動する")
    p.add_argument("rev", metavar="<版>", help="版番号、@、ブランチ名など(リビジョン式)")
    p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)

    p = sub.add_parser("note", help="版にコメントを追記する")
    p.add_argument("-m", "--message", dest="text", required=True, metavar="<本文>", help="コメントの本文")
    p.add_argument("-r", dest="rev", default="@", metavar="<版>", help="対象の版(省略時は @)")

    p = sub.add_parser("branch", help="ブランチの一覧・名前の付け外し")
    bsub = p.add_subparsers(dest="branch_command", metavar="<操作>", title="操作(省略時は一覧)")
    bp = bsub.add_parser("name", help="版が属するブランチに名前を付ける(既存の名前は付け替える)")
    bp.add_argument("name", metavar="<名前>", help="ブランチ名")
    bp.add_argument("rev", nargs="?", default="@", metavar="<版>", help="対象の版(省略時は @)")
    bp = bsub.add_parser("unname", help="名前を外す")
    bp.add_argument("name", metavar="<名前>", help="ブランチ名")

    p = sub.add_parser("discard", help="版に削除の印を付ける(データは gc まで残る)")
    p.add_argument("rev", nargs="?", default="@", metavar="<版>", help="対象の版(省略時は @)")
    p.add_argument("--force", action="store_true", help="git のコミットが参照している版でも削除する")
    p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)

    p = sub.add_parser("gc", help="削除済みの版と不要なデータを消す")
    p.add_argument("--dry-run", action="store_true", help="削除対象と容量を表示するだけで、何も削除しない")
    p.add_argument("--no-git", action="store_true", help="git の履歴による保護を省く")

    p = sub.add_parser("verify", help="保存データを検査する(異常が残れば終了コード 1)")
    p.add_argument("--quick", action="store_true", help="チャンクの存在とヘッダだけを確認する")
    p.add_argument("--repair", action="store_true", help="壊れたデータを作業フォルダのファイルから修復する")

    return parser


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    # 引数を解析してコマンドを実行し、終了コードを返す(M2-11、仕様書 2.2節)。
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE

    setup_logging(quiet=args.quiet or args.json)

    if args.help:
        parser.print_help()
        return EXIT_OK
    if args.version:
        print(f"bvc {__version__}")
        return EXIT_OK
    if not args.command:
        parser.print_usage(sys.stderr)
        logger.error("コマンドを指定してください")
        return EXIT_USAGE

    start = args.workdir if args.workdir is not None else Path.cwd()
    try:
        if args.command == "init":
            return _cmd_init(args, start)
        if args.command == "commit":
            return _cmd_commit(args, start)
        if args.command == "log":
            return _cmd_log(args, start)
        if args.command in ("undo", "redo", "goto"):
            return _cmd_move(args, start)
        if args.command == "note":
            return _cmd_note(args, start)
        if args.command == "branch":
            return _cmd_branch(args, start)
        if args.command == "discard":
            return _cmd_discard(args, start)
        if args.command == "gc":
            return _cmd_gc(args, start)
        if args.command == "verify":
            return _cmd_verify(args, start)
        logger.error("不明なコマンドです: %s", args.command)
        return EXIT_USAGE
    except BvcError as e:
        if args.json:
            _print_json(
                {
                    "changed": False,
                    "error": str(e),
                    "type": type(e).__name__,
                    "exit_code": e.exit_code,
                    "details": e.details,
                }
            )
        prefix = "中止" if isinstance(e, SafetyAbort) else None
        logger.error("%s", e, extra={"prefix": prefix})
        return e.exit_code
    except KeyboardInterrupt:
        logger.error("中断しました")
        return EXIT_ABORT
    except Exception as e:
        logger.error("予期しないエラー: %s", e, exc_info=True)
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace, start: Path) -> int:
    workdir = start / args.path if args.path else start
    with Repo.init(workdir, track=args.track, ignore=args.ignore) as repo:
        entry = repo.log(limit=1)[0]
    files = len(entry.commit.tree)
    if args.json:
        _print_json(
            {
                "changed": True,
                "path": str(repo.workdir),
                "commit": entry.id,
                "files": files,
            }
        )
    else:
        logger.info(
            "リポジトリを作成しました: %s(版 %d、追跡ファイル %d 件)",
            repo.workdir,
            entry.id,
            files,
        )
    return EXIT_OK


def _cmd_commit(args: argparse.Namespace, start: Path) -> int:
    # --rename 旧=新(パスの検査と正規化は repo 層で行う)
    renames = []
    for spec in args.renames or ():
        old, sep, new = spec.partition("=")
        if not (sep and old and new):
            raise UsageError(f"--rename は 旧=新 の形式で指定してください: {spec}")
        renames.append((old, new))
    with Repo.open(start) as repo:
        result = repo.commit(message=args.message, allow_missing=args.allow_missing, renames=renames)
    if args.json:
        _print_json(result)
        return EXIT_OK
    if not result.changed:
        logger.info("変更なし")
        return EXIT_OK
    s = result.state
    logger.info(
        "版 %d を作成しました%s(新規データ %s / %s)",
        result.commit.id,
        "(新しいブランチを作成)" if result.new_branch else "",
        format_size(s.new_bytes),
        format_size(s.total_bytes),
    )
    for line in _change_lines(s, deleted_label="deleted"):
        logger.info("  %s", line)
    return EXIT_OK


def _cmd_log(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        entries = repo.log(include_discarded=args.discarded, limit=args.limit)
        try:
            state: WorkState | None = repo.work_state()
        except BvcError as e:
            logger.warning("未コミットの変更を確認できません: %s", e)
            state = None
    if args.json:
        _print_json(
            {
                "changed": False,
                "uncommitted": None
                if state is None
                else {
                    "modified": state.modified,
                    "added": state.added,
                    "renamed": [list(r) for r in state.renamed],
                    "missing": state.missing,
                    "hints": state.hints,
                },
                "entries": [_entry_to_dict(e) for e in entries],
            }
        )
    else:
        print(format_log(entries, state))
    return EXIT_OK


def _cmd_move(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        if args.command == "undo":
            result = repo.undo(
                reason=args.reason, allow_missing=args.allow_missing, skip_broken=args.skip_broken
            )
        elif args.command == "redo":
            result = repo.redo(
                reason=args.reason, allow_missing=args.allow_missing, skip_broken=args.skip_broken
            )
        else:
            result = repo.goto(args.rev, allow_missing=args.allow_missing)
    if args.json:
        _print_json(result)
        return EXIT_OK
    for line in format_move(result):
        logger.info("%s", line)
    return EXIT_OK


def _cmd_note(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        note = repo.note(args.text, rev=args.rev)
        commit = repo.get_commit(note.commit_id)
    if args.json:
        _print_json({"changed": True, "note": asdict(note)})
    else:
        logger.info("版 %d(%s)にコメントを追加しました", note.commit_id, _commit_label(commit))
    return EXIT_OK


def _cmd_branch(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        if args.branch_command is None:
            branches = repo.branches()
        elif args.branch_command == "name":
            info = repo.name_branch(args.name, rev=args.rev)
        else:
            info = repo.unname_branch(args.name)
    if args.branch_command is None:
        if args.json:
            _print_json({"changed": False, "branches": [asdict(b) for b in branches]})
        else:
            print(format_branches(branches))
        return EXIT_OK
    if args.json:
        _print_json({"changed": True, "branch": asdict(info)})
    elif args.branch_command == "name":
        logger.info("ブランチ(先端 %s)に名前 '%s' を付けました", _or_none(info.tip), args.name)
    else:
        logger.info("ブランチ(先端 %s)から名前 '%s' を外しました", _or_none(info.tip), args.name)
    return EXIT_OK


def _cmd_discard(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        result = repo.discard(rev=args.rev, force=args.force, allow_missing=args.allow_missing)
    if args.json:
        _print_json(result)
        return EXIT_OK
    for line in format_discard(result):
        logger.info("%s", line)
    return EXIT_OK


def _cmd_gc(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        result = repo.gc(dry_run=args.dry_run, no_git=args.no_git)
    if args.json:
        _print_json(result)
        return EXIT_OK
    logger.info("%s", format_gc(result))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace, start: Path) -> int:
    # 異常が残っていれば終了コード 1(仕様書 3.10節)。
    with Repo.open(start) as repo:
        result = repo.verify(quick=args.quick, repair=args.repair)
    code = EXIT_OK if result.ok else EXIT_ERROR
    if args.json:
        _print_json({**asdict(result), "ok": result.ok})
        return code
    lines = format_verify(result)
    for line in lines[:-1] if not result.ok else lines:
        logger.info("%s", line)
    if not result.ok:
        logger.error("%s", lines[-1])
    return code


# ---------------------------------------------------------------------------
# 表示の整形
# ---------------------------------------------------------------------------


def format_move(r: MoveResult) -> list[str]:
    if not r.changed:
        return [f"変更なし(現在位置は版 {r.after.at} です)"]
    if r.after.at == r.before.at:
        return [f"版 {r.after.at} のまま、現在のブランチを切り替えました"]
    lines = [f"版 {r.after.at} に移動しました"]
    if r.skipped:
        lines.append(f"  壊れた版 {', '.join(map(str, r.skipped))} を飛ばしました")
    if r.auto_commit is not None:
        lines.append(f"  未コミットの変更を版 {r.auto_commit.id} に自動コミットしました({r.auto_commit.message})")
    lines += [f"  restored: {p}" for p in r.restored]
    lines += [f"  deleted:  {p}" for p in r.deleted]
    return lines


def _or_none(v: int | None) -> str:
    return "なし" if v is None else str(v)


def _commit_label(c: Commit | None) -> str:
    if c is None:
        return "読み込み不可"
    return c.message or c.kind


def _width(s: str) -> int:
    # 端末での表示幅(全角は2)。
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def format_branches(branches: list[BranchInfo]) -> str:
    # 仕様書 3.7節。現在のブランチに '*' を付ける。内部のブランチ番号は表示しない。
    names = [b.name or "(名前なし)" for b in branches]
    w = max((_width(n) for n in names), default=0)
    lines = []
    for b, n in zip(branches, names):
        mark = "*" if b.is_current else " "
        lines.append(f"{mark} {n}{' ' * (w - _width(n))}  先端 {_or_none(b.tip)}  分岐元 {_or_none(b.fork)}")
    return "\n".join(lines)


def format_discard(r: DiscardResult) -> list[str]:
    lines = [f"版 {r.discarded} に削除の印を付けました"]
    if r.after.at != r.before.at:
        lines.append(f"  版 {r.after.at} に移動しました")
        if r.auto_commit is not None:
            lines.append(f"  未コミットの変更を版 {r.auto_commit.id} に自動コミットしました({r.auto_commit.message})")
        lines += [f"  restored: {p}" for p in r.restored]
        lines += [f"  deleted:  {p}" for p in r.deleted]
    return lines


def format_gc(r: GcReport) -> str:
    total = len(r.deleted_commits) + r.deleted_manifests + r.deleted_chunks + r.deleted_tmp
    if total == 0:
        return "削除対象がありません"
    versions = ", ".join(map(str, r.deleted_commits)) or "なし"
    detail = (
        f"版 {versions}、マニフェスト {r.deleted_manifests}、チャンク {r.deleted_chunks}、"
        f"一時ファイル {r.deleted_tmp}(合計 {format_size(r.freed_bytes)})"
    )
    return f"削除対象: {detail}" if r.dry_run else f"削除しました: {detail}"


def format_verify(r: VerifyReport) -> list[str]:
    # 最後の行が結果(異常が残っていれば、壊れた版の一覧)。
    lines = [
        f"検査しました{'(--quick)' if r.quick else ''}: チャンク {r.checked_chunks}、"
        f"マニフェスト {r.checked_manifests}、版 {r.checked_commits}"
    ]
    if r.repaired_chunks or r.repaired_manifests:
        lines.append(
            f"  修復しました: チャンク {len(r.repaired_chunks)}、マニフェスト {len(r.repaired_manifests)}"
        )
    if r.bad_chunks or r.bad_manifests:
        lines.append(f"  欠損・破損: チャンク {len(r.bad_chunks)}、マニフェスト {len(r.bad_manifests)}")
    if r.ok:
        lines.append("異常はありません")
    else:
        hint = "" if r.repair else "(verify --repair で修復できる場合があります)"
        lines.append(f"壊れた版があります: {', '.join(map(str, r.broken_commits))}{hint}")
    return lines


def format_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _change_lines(s: WorkState, deleted_label: str = "missing") -> list[str]:
    lines = [f"modified: {p}" for p in s.modified]
    lines += [f"added:    {p}" for p in s.added]
    # 類似による名前変更だけ類似度を付ける(完全一致・手動指定は 1.0。切り捨てで 100% と紛れない)
    lines += [
        f"renamed:  {a} → {b}" + (f" ({int(sim * 100)}%)" if sim < 1.0 else "")
        for a, b, sim in s.renamed
    ]
    lines += [f"{deleted_label}:  {p}" for p in s.missing]
    return lines


def _short_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except ValueError:
        return iso


def _entry_text(e: LogEntry) -> str:
    if e.commit is None:
        return f"{e.id}  (読み込み不可)" + ("  (削除済み)" if e.discarded else "")
    c = e.commit
    parts = [str(e.id)]
    if e.branch_label:
        parts.append(f"[{e.branch_label}]")
    parts.append(_short_time(c.time))
    parts.append(c.message or f"({c.kind})")
    if e.discarded:
        parts.append("(削除済み)")
    if e.broken:
        parts.append("(壊れた版)")
    return "  ".join(parts)


def _lane_cells(lanes: list[int | None]) -> list[str]:
    return ["│" if lane is not None else " " for lane in lanes]


def _join(cells: list[str], fill_from: int = -1, fill_to: int = -1) -> str:
    # セルを空白でつなぐ。fill_from < i <= fill_to の区切りは '─' にする(合流の横線)。
    out = cells[0] if cells else ""
    for i in range(1, len(cells)):
        out += ("─" if fill_from < i <= fill_to else " ") + cells[i]
    return out.rstrip()


def _graph_lines(entries: list[LogEntry]) -> list[str]:
    # 版を新しい順に並べ、枝の列を割り当ててツリーを描く(M2-12、仕様書 3.3節)。
    # lanes[i] は列 i が次に待っている版の番号(None は空き)。
    lanes: list[int | None] = []
    lines: list[str] = []
    for e in entries:
        cols = [i for i, lane in enumerate(lanes) if lane == e.id]
        if cols:
            col = cols[0]
        elif None in lanes:
            col = lanes.index(None)
        else:
            col = len(lanes)
            lanes.append(None)
        for i in cols[1:]:
            lanes[i] = None
        lanes[col] = e.id

        cells = _lane_cells(lanes)
        cells[col] = "@" if e.is_current else ("✗" if e.broken else "○")
        lines.append(f"{_join(cells)}  {_entry_text(e)}")

        parent = e.effective_parent
        lanes[col] = parent
        for n in e.notes:
            lines.append(
                f"{_join(_lane_cells(lanes)).ljust(len(lanes) * 2 - 1)}     note: {n.text}"
            )

        # 同じ親を待つ列があれば、左側の列へ合流させる
        if parent is not None:
            same = sorted(i for i, lane in enumerate(lanes) if lane == parent)
            target = same[0]
            for src in same[1:]:
                cells = _lane_cells(lanes)
                cells[target] = "├"
                cells[src] = "╯"
                for i in range(target + 1, src):
                    cells[i] = "┼" if lanes[i] is not None else "─"
                lines.append(_join(cells, target, src))
                lanes[src] = None
        while lanes and lanes[-1] is None:
            lanes.pop()
    return lines


def format_log(entries: list[LogEntry], state: WorkState | None) -> str:
    lines = []
    if state is not None and state.dirty:
        changes = [f"{p} (modified)" for p in state.modified]
        changes += [f"{p} (added)" for p in state.added]
        changes += [f"{a} → {b} (renamed)" for a, b, _ in state.renamed]
        changes += [f"{p} (missing)" for p in state.missing]
        lines.append("未コミットの変更: " + ", ".join(changes))
    lines += _graph_lines(entries)
    return "\n".join(lines)


def _entry_to_dict(e: LogEntry) -> dict:
    c = e.commit
    return {
        "id": e.id,
        "commit": None
        if c is None
        else {
            "id": c.id,
            "parent": c.parent,
            "branch": c.branch,
            "time": c.time,
            "kind": c.kind,
            "message": c.message,
        },
        "effective_parent": e.effective_parent,
        "branch_label": e.branch_label,
        "is_tip": e.is_tip,
        "is_current": e.is_current,
        "broken": e.broken,
        "discarded": e.discarded,
        "pinned": e.pinned,
        "notes": [{"time": n.time, "text": n.text} for n in e.notes],
    }


def _print_json(result: Any) -> None:
    data = asdict(result) if is_dataclass(result) else result
    print(json.dumps(data, ensure_ascii=False, indent=2))
