# 引数解析・表示整形・終了コード・--json。設計書 1.1節・3.7節・3.9節、仕様書 2.11節。
# 情報・警告・エラーのメッセージは logging で出す(情報は stdout、警告以上は stderr)。
# コマンドの結果そのもの(log の表示、--json、--version)は stdout へ書く。
# --json では、結果・警告(warnings)・エラーをすべて stdout の1つの JSON にまとめる(M5-1, M5-3)。
# 進捗は、標準エラーが端末のときだけ表示する(M5-2)。

from __future__ import annotations

import argparse
import codecs
import dataclasses
import json
import logging
import shutil
import sys
import time
import traceback
import unicodedata
from datetime import datetime
from functools import cache
from pathlib import Path, PurePath
from typing import Any, ClassVar, Final, TextIO

from . import __version__
from .errors import BvcError, SafetyAbort, UsageError
from .fsutil import BVC_DIR
from .model import (
    BranchInfo,
    Commit,
    DiscardResult,
    GcReport,
    HooksResult,
    LogEntry,
    MoveResult,
    ProgressEvent,
    SyncResult,
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


class _StreamHandler(logging.StreamHandler):
    # 出力先の文字コードで表せない記号を置き換えてから書く(_fallback)。
    def format(self, record: logging.LogRecord) -> str:
        return _fallback(super().format(record), self.stream)


class _WarningCollector(logging.Handler):
    # --json のとき、警告以上のメッセージを集める(出力の "warnings" 配列。M5-3)。
    def __init__(self, sink: list[str]) -> None:
        super().__init__(logging.WARNING)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record.getMessage())


class _ClearProgress(logging.Filter):
    # メッセージを書く前に進捗の行を消す(進捗とメッセージが同じ行に混ざらないように)。
    def __init__(self, view: ProgressView) -> None:
        super().__init__()
        self.view = view

    def filter(self, record: logging.LogRecord) -> bool:
        self.view.clear()
        return True


def setup_logging(
    quiet: bool = False,
    warnings: list[str] | None = None,
    progress: ProgressView | None = None,
) -> None:
    # bvc パッケージのロガーの出力先を設定する。呼び出すたびに作り直す(その時点の sys.stdout/stderr を使う)。
    # warnings を渡すと(--json)、画面には何も出さず、警告以上のメッセージをそこに集める。
    root = logging.getLogger("bvc")
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    root.propagate = False
    if warnings is not None:
        root.addHandler(_WarningCollector(warnings))
        return
    fmt = _Formatter("%(message)s")
    handlers: list[logging.Handler] = []
    if not quiet:
        out = _StreamHandler(sys.stdout)
        out.setLevel(logging.INFO)
        out.addFilter(lambda r: r.levelno < logging.WARNING)
        handlers.append(out)
    err = _StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    handlers.append(err)
    for h in handlers:
        h.setFormatter(fmt)
        if progress is not None:
            h.addFilter(_ClearProgress(progress))
        root.addHandler(h)


# ---------------------------------------------------------------------------
# 出力の文字コード
# ---------------------------------------------------------------------------

# 表示に使う記号と、出力先の文字コードで表せないときの代わり(先頭から順に試す)。
# パイプ・リダイレクト先が cp932 の場合など(M4-C の残課題)。
_SYMBOL_FALLBACKS: Final[dict[str, tuple[str, ...]]] = {
    "✗": ("×", "x"),
    "╯": ("┘", "/"),
    "├": ("|",),
    "┼": ("+",),
    "─": ("-",),
    "│": ("|",),
    "○": ("o",),
    "→": ("->",),
}


@cache
def _symbol_table(encoding: str) -> dict[int, str]:
    # encoding で表せない記号 → 代わりの文字列(str.translate 用)。
    table: dict[int, str] = {}
    for sym, alts in _SYMBOL_FALLBACKS.items():
        for cand in (sym, *alts):
            try:
                cand.encode(encoding)
            except UnicodeEncodeError:
                continue
            if cand != sym:
                table[ord(sym)] = cand
            break
    return table


def _encoding_of(stream: Any) -> str:
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        return codecs.lookup(enc).name
    except LookupError:
        return "utf-8"


def _fallback(text: str, stream: Any) -> str:
    # 表示用の記号のうち、出力先で表せないものを置き換える。
    return text.translate(_symbol_table(_encoding_of(stream)))


def _prepare_streams() -> None:
    # 出力先で表せない文字(ファイル名など)で UnicodeEncodeError にならないよう、'?' に置き換える設定にする。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError, TypeError):
            pass


def _print_text(text: str) -> None:
    print(_fallback(text, sys.stdout))


# ---------------------------------------------------------------------------
# 進捗表示(M5-2)
# ---------------------------------------------------------------------------

_STAGE_LABELS: Final[dict[str, str]] = {
    "put": "保存",
    "write": "展開",
    "restore": "置き換え",
    "verify_chunks": "チャンクの検査",
    "verify_manifests": "マニフェストの検査",
    "repair": "修復",
    "gc": "削除",
}
# done・total がバイト数の段階(それ以外は件数)
_BYTE_STAGES: Final[frozenset[str]] = frozenset({"put", "write"})
# 表示を更新する最短の間隔(秒)。段階やファイルが変わったときはすぐに更新する
PROGRESS_INTERVAL: Final[float] = 0.1


class ProgressView:
    # ProgressEvent を受けて、標準エラーの1行に進捗を上書き表示する。
    # 端末でないとき・-q・--json では作らない(run の中で判断する)。

    def __init__(self, stream: TextIO, interval: float = PROGRESS_INTERVAL) -> None:
        self.stream = stream
        self.interval = interval
        self._last_time = 0.0
        self._last_key: tuple[str, str | None] | None = None
        self._shown = 0  # 表示中の行の幅(0 なら何も表示していない)

    def __call__(self, ev: ProgressEvent) -> None:
        now = time.monotonic()
        key = (ev.stage, ev.path)
        if key == self._last_key and now - self._last_time < self.interval:
            return
        self._last_key = key
        self._last_time = now
        self._show(format_progress(ev))

    def _show(self, line: str) -> None:
        cols = shutil.get_terminal_size((80, 24)).columns - 1
        line = _truncate(_fallback(line, self.stream), max(cols, 10))
        w = _width(line)
        pad = " " * max(self._shown - w, 0)
        self.stream.write(f"\r{line}{pad}")
        self.stream.flush()
        self._shown = w

    def clear(self) -> None:
        if self._shown:
            self.stream.write("\r" + " " * self._shown + "\r")
            self.stream.flush()
            self._shown = 0
        self._last_key = None


def format_progress(ev: ProgressEvent) -> str:
    label = _STAGE_LABELS.get(ev.stage, ev.stage)
    if ev.stage in _BYTE_STAGES:
        amount = format_size(ev.done)
        if ev.total is not None:
            amount += f" / {format_size(ev.total)}"
    else:
        amount = str(ev.done) if ev.total is None else f"{ev.done}/{ev.total}"
    pct = f" ({ev.done * 100 // ev.total}%)" if ev.total else ""
    path = f" {ev.path}" if ev.path else ""
    return f"{label}{path}: {amount}{pct}"


def _truncate(s: str, cols: int) -> str:
    # 表示幅が cols を超えるなら、先頭を削って "…" を付ける(末尾の数値を残す)。
    if _width(s) <= cols:
        return s
    out = ""
    for ch in reversed(s):
        if _width(out) + _width(ch) > cols - 1:
            break
        out = ch + out
    return "…" + out


# ---------------------------------------------------------------------------
# --json(M5-1)
# ---------------------------------------------------------------------------


def to_jsonable(obj: Any) -> Any:
    # 結果の dataclass を JSON に変換できる値にする(全コマンド共通)。
    # dataclass はフィールドに加えて、公開のプロパティ(WorkState.dirty, VerifyReport.ok など)も含める。
    # タプルはリスト、Path は文字列、辞書のキーは文字列にする。
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {
            f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)
        }
        for name in _public_properties(type(obj)):
            out[name] = to_jsonable(getattr(obj, name))
        return out
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((to_jsonable(v) for v in obj), key=str)
    if isinstance(obj, PurePath):
        return str(obj)
    return str(obj)


@cache
def _public_properties(cls: type) -> tuple[str, ...]:
    names = []
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if (
                isinstance(value, property)
                and not name.startswith("_")
                and name not in names
            ):
                names.append(name)
    return tuple(names)


def _print_json(args: argparse.Namespace, result: Any) -> None:
    # 結果を1つの JSON として stdout に書く。"changed" と "warnings" を必ず含める(仕様書 2.2節・5節)。
    data = to_jsonable(result)
    if not isinstance(data, dict) or "changed" not in data:
        raise TypeError(f"--jsonの出力にchangedがありません: {type(result).__name__}")
    data["warnings"] = list(getattr(args, "warnings", None) or ())
    # 出力先が UTF-8 でなければ ASCII だけで書く(\\uXXXX。どの文字コードでも解析できる)
    ascii_only = _encoding_of(sys.stdout) not in ("utf-8", "utf-16", "utf-32")
    print(json.dumps(data, ensure_ascii=ascii_only, indent=2))


def _error_json(e: BaseException, exit_code: int, message: str | None = None) -> dict:
    details = e.details if isinstance(e, BvcError) else {}
    return {
        "changed": False,
        "error": message if message is not None else str(e),
        "type": type(e).__name__,
        "exit_code": exit_code,
        "details": details,
    }


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    # 引数の誤りを SystemExit ではなく UsageError で報告する(--json でも JSON で返すため)。
    # usage はそのサブコマンドのもの(details には入れず、テキスト表示のときだけ使う)。
    def error(self, message: str) -> None:  # type: ignore[override]
        e = UsageError(message)
        e.usage = self.format_usage()  # type: ignore[attr-defined]
        raise e


def build_parser() -> argparse.ArgumentParser:
    # argparse を構成する(M2-11)。サブコマンドのパーサも _ArgumentParser になる。
    parser = _ArgumentParser(
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
        help=f"作業フォルダを指定する(省略時はカレントから上位へ{BVC_DIR}を探す)",
    )
    parser.add_argument("--json", action="store_true", help="結果をJSONで出力する")
    parser.add_argument(
        "-q", dest="quiet", action="store_true", help="通常の出力を抑制する"
    )
    parser.add_argument("--help", action="store_true", help="ヘルプを表示")
    parser.add_argument("--version", action="store_true", help="バージョンを表示")

    sub = parser.add_subparsers(dest="command", metavar="<コマンド>", title="コマンド")

    p = sub.add_parser("init", help="リポジトリを作成する")
    p.add_argument(
        "--track",
        action="extend",
        nargs="+",
        required=True,
        metavar="<パターン>",
        help="追跡パターン(複数指定可) 例: --track '*.bin' '*.exe'",
    )
    p.add_argument(
        "--ignore",
        action="extend",
        nargs="+",
        default=[],
        metavar="<パターン>",
        help="除外パターン(複数指定可) 例: --ignore '*.txt' '*.log'",
    )
    p.add_argument(
        "--git",
        action="store_true",
        help="git連携を有効にし、bvc.lockを作ってフックを設置する",
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
    for name, help_text in (
        ("undo", "1つ前の版に戻る"),
        ("redo", "戻したのを取り消す(先端へ進む)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "-m",
            "--message",
            dest="reason",
            default="",
            metavar="<理由>",
            help="理由(操作ログに記録する)",
        )
        p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)
        p.add_argument(
            "--skip-broken",
            action="store_true",
            help="壊れた版を飛ばして、同じ方向で最も近い健全な版へ移動する",
        )

    p = sub.add_parser("goto", help="指定の版へ移動する")
    p.add_argument(
        "rev", metavar="<版>", help="版番号、@、ブランチ名など(リビジョン式)"
    )
    p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)

    p = sub.add_parser("note", help="版にコメントを追記する")
    p.add_argument(
        "-m",
        "--message",
        dest="text",
        required=True,
        metavar="<本文>",
        help="コメントの本文",
    )
    p.add_argument(
        "-r", dest="rev", default="@", metavar="<版>", help="対象の版(省略時は@)"
    )

    p = sub.add_parser("branch", help="ブランチの一覧・名前の付け外し")
    bsub = p.add_subparsers(
        dest="branch_command", metavar="<操作>", title="操作(省略時は一覧)"
    )
    bp = bsub.add_parser(
        "name", help="版が属するブランチに名前を付ける(既存の名前は付け替える)"
    )
    bp.add_argument("name", metavar="<名前>", help="ブランチ名")
    bp.add_argument(
        "rev", nargs="?", default="@", metavar="<版>", help="対象の版(省略時は@)"
    )
    bp = bsub.add_parser("unname", help="名前を外す")
    bp.add_argument("name", metavar="<名前>", help="ブランチ名")

    p = sub.add_parser("discard", help="版に削除の印を付ける(データはgcまで残る)")
    p.add_argument(
        "rev", nargs="?", default="@", metavar="<版>", help="対象の版(省略時は@)"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="gitのコミットが参照している版でも削除する",
    )
    p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)

    p = sub.add_parser("gc", help="削除済みの版と不要なデータを消す")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="削除対象と容量を表示するだけで、何も削除しない",
    )
    p.add_argument("--no-git", action="store_true", help="gitの履歴による保護を省く")

    p = sub.add_parser("verify", help="保存データを検査する(異常があれば終了コード1)")
    p.add_argument(
        "--quick", action="store_true", help="チャンクの存在とヘッダだけを確認する"
    )
    p.add_argument(
        "--repair",
        action="store_true",
        help="壊れたデータを作業フォルダのファイルから修復する(完全な修復は保証しない)",
    )

    p = sub.add_parser("sync", help="bvc.lock の内容に作業ファイルを合わせる(git連携)")
    p.add_argument("--allow-missing", action="store_true", help=allow_missing_help)
    p.add_argument(
        "--require-lock",
        action="store_true",
        help="bvc.lockが無ければ終了コード1にする",
    )

    p = sub.add_parser("git", help="git連携(フックの設置、フック用のコマンド)")
    gsub = p.add_subparsers(dest="git_command", metavar="<操作>", title="操作")
    gsub.required = True
    gsub.add_parser(
        "install-hooks", help="pre-commit/post-commit/post-checkoutを設置する"
    )
    gsub.add_parser("pin", help="(フック用)gitのHEADとbvcの版の対応を記録する")
    gsub.add_parser("pre-commit", help="(フック用)")
    gp = gsub.add_parser("post-checkout", help="(フック用)")
    gp.add_argument(
        "hook_args",
        nargs="*",
        metavar="<引数>",
        help="git が渡す引数(前のHEAD、新しい HEAD、フラグ)",
    )

    return parser


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    # 引数を解析してコマンドを実行し、終了コードを返す(M2-11、仕様書 2.2節)。
    _prepare_streams()
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw)
    except SystemExit as e:
        # サブコマンドの --help など
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    except UsageError as e:
        # 解析できなかったので、共通オプションの --json は引数の並びから判断する
        args = argparse.Namespace(json="--json" in raw, quiet="-q" in raw)
        args.warnings = [] if args.json else None
        setup_logging(quiet=args.quiet, warnings=args.warnings)
        return _report_error(args, e, usage=getattr(e, "usage", None))

    args.warnings = [] if args.json else None
    view = None
    if not (args.quiet or args.json) and _isatty(sys.stderr):
        view = ProgressView(sys.stderr)
    args.progress = view
    setup_logging(quiet=args.quiet, warnings=args.warnings, progress=view)

    if args.help:
        parser.print_help()
        return EXIT_OK
    if args.version:
        if args.json:
            _print_json(args, {"changed": False, "version": __version__})
        else:
            print(f"bvc {__version__}")
        return EXIT_OK
    if not args.command:
        return _report_error(
            args, UsageError("コマンドを指定してください"), usage=parser.format_usage()
        )

    start = args.workdir if args.workdir is not None else Path.cwd()
    try:
        return _COMMANDS[args.command](args, start)
    except BvcError as e:
        return _report_error(args, e)
    except KeyboardInterrupt as e:
        return _report_error(args, e, EXIT_ABORT, "中断しました")
    except Exception as e:
        if args.json:
            traceback.print_exc(file=sys.stderr)
        else:
            logger.exception(f"予期しないエラー: {e}")  # noqa: TRY401
        return _report_error(args, e, EXIT_ERROR, f"予期しないエラー: {e}", logged=True)
    finally:
        if view is not None:
            view.clear()


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _report_error(
    args: argparse.Namespace,
    e: BaseException,
    exit_code: int | None = None,
    message: str | None = None,
    usage: str | None = None,
    logged: bool = False,
) -> int:
    # エラーを報告して終了コードを返す。--json なら stdout に JSON、そうでなければ stderr にメッセージ。
    if exit_code is None:
        exit_code = e.exit_code if isinstance(e, BvcError) else EXIT_ERROR
    if args.json:
        _print_json(args, _error_json(e, exit_code, message))
        return exit_code
    if logged:
        return exit_code
    if usage:
        sys.stderr.write(usage)
    prefix = "中止" if isinstance(e, SafetyAbort) else None
    logger.error(f"{message or e}", extra={"prefix": prefix})
    return exit_code


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace, start: Path) -> int:
    hooks: HooksResult | None = None
    with Repo.init(
        start,
        track=args.track,
        ignore=args.ignore,
        git=args.git,
        progress=args.progress,
    ) as repo:
        entry = repo.log(limit=1)[0]
        if args.git:
            # フックの設置に失敗しても init は成功とする(後から install-hooks で設置できる)
            try:
                hooks = repo.install_hooks()
            except BvcError as e:
                logger.warning(
                    f"gitのフックを設置できませんでした: {e}(bvc git install-hooksで設置できます)"
                )
    commit = entry.commit
    if args.json:
        _print_json(
            args,
            {
                "changed": True,
                "workdir": repo.workdir,
                "commit": commit,
                "hooks": hooks,
            },
        )
        return EXIT_OK
    tracked = len(commit.tree) if commit is not None else 0
    logger.info(
        f"リポジトリを作成しました: {repo.workdir}(版{entry.id}、追跡ファイル{tracked}件)"
    )
    if args.git:
        lock_file = repo.config.git.lock_file
        logger.info(
            f"git連携を有効にしました。{lock_file}をgit addしてコミットしてください"
        )
        if hooks is not None:
            _log_hooks(hooks)
        logger.info(".gitignoreに以下のファイルを追加して下さい:")
        for line in [f"/{BVC_DIR}/", *args.track]:
            logger.info(f"  {line}")
    return EXIT_OK


def _cmd_commit(args: argparse.Namespace, start: Path) -> int:
    # --rename 旧=新(パスの検査と正規化は repo 層で行う)
    renames = []
    for spec in args.renames or ():
        old, sep, new = spec.partition("=")
        if not (sep and old and new):
            raise UsageError(f"--renameは 旧=新 の形式で指定してください: {spec}")
        if "=" in old or "=" in new:
            raise UsageError(
                f"--rename: 旧・新のパスに'='は使えません(旧=新の区切りと区別できないため): {spec}"
            )
        renames.append((old, new))
    with Repo.open(start) as repo:
        result = repo.commit(
            message=args.message,
            allow_missing=args.allow_missing,
            renames=renames,
            progress=args.progress,
        )
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    if not result.changed:
        logger.info("変更がありません")
        return EXIT_OK
    s = result.state
    new_branch = "(新しいブランチを作成)" if result.new_branch else ""
    logger.info(
        f"版{result.commit.id}を作成しました{new_branch}"
        f"(新規データ {format_size(s.new_bytes)}/{format_size(s.total_bytes)})"
    )
    for line in _change_lines(s, deleted_label="deleted"):
        logger.info(f"  {line}")
    return EXIT_OK


def _cmd_log(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        entries = repo.log(include_discarded=args.discarded, limit=args.limit)
        try:
            state: WorkState | None = repo.work_state()
        except BvcError as e:
            logger.warning(f"未コミットの変更を確認できません: {e}")
            state = None
    if args.json:
        _print_json(
            args,
            {
                "changed": False,
                "uncommitted": state,
                "entries": entries,
                "lock_status": repo.lock_status,
            },
        )
    else:
        _print_text(format_log(entries, state))
    return EXIT_OK


def _cmd_move(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        if args.command == "undo":
            result = repo.undo(
                reason=args.reason,
                allow_missing=args.allow_missing,
                skip_broken=args.skip_broken,
                progress=args.progress,
            )
        elif args.command == "redo":
            result = repo.redo(
                reason=args.reason,
                allow_missing=args.allow_missing,
                skip_broken=args.skip_broken,
                progress=args.progress,
            )
        else:
            result = repo.goto(
                args.rev, allow_missing=args.allow_missing, progress=args.progress
            )
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    for line in format_move(result):
        logger.info(line)
    return EXIT_OK


def _cmd_note(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        note = repo.note(args.text, rev=args.rev)
        commit = repo.get_commit(note.commit_id)
    if args.json:
        _print_json(args, {"changed": True, "note": note})
    else:
        logger.info(
            f"版{note.commit_id}({_commit_label(commit)})にコメントを追加しました"
        )
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
            _print_json(args, {"changed": False, "branches": branches})
        else:
            _print_text(format_branches(branches))
        return EXIT_OK
    if args.json:
        _print_json(args, {"changed": True, "branch": info})
    elif args.branch_command == "name":
        logger.info(
            f"ブランチ(先端 {_or_none(info.tip)})に名前'{args.name}'を付けました"
        )
    else:
        logger.info(
            f"ブランチ(先端{_or_none(info.tip)})から名前'{args.name}'を外しました"
        )
    return EXIT_OK


def _cmd_discard(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        result = repo.discard(
            rev=args.rev,
            force=args.force,
            allow_missing=args.allow_missing,
            progress=args.progress,
        )
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    for line in format_discard(result):
        logger.info(line)
    return EXIT_OK


def _cmd_gc(args: argparse.Namespace, start: Path) -> int:
    with Repo.open(start) as repo:
        result = repo.gc(
            dry_run=args.dry_run, no_git=args.no_git, progress=args.progress
        )
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    logger.info(format_gc(result))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace, start: Path) -> int:
    # 異常が残っていれば終了コード 1(仕様書 3.10節)。
    with Repo.open(start) as repo:
        result = repo.verify(
            quick=args.quick, repair=args.repair, progress=args.progress
        )
    code = EXIT_OK if result.ok else EXIT_ERROR
    if args.json:
        _print_json(args, result)
        return code
    lines = format_verify(result)
    for line in lines[:-1] if not result.ok else lines:
        logger.info(line)
    if not result.ok:
        logger.error(lines[-1])
    return code


def _cmd_sync(args: argparse.Namespace, start: Path) -> int:
    # これから合わせるので、非同期状態の警告は出さない
    with Repo.open(start, warn_out_of_sync=False) as repo:
        result = repo.sync(
            allow_missing=args.allow_missing,
            require_lock=args.require_lock,
            progress=args.progress,
        )
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    for line in format_sync(result):
        logger.info(line)
    return EXIT_OK


def _cmd_git(args: argparse.Namespace, start: Path) -> int:
    cmd = args.git_command
    if cmd == "post-checkout":
        return _git_post_checkout(args, start)
    with Repo.open(start) as repo:
        if cmd == "install-hooks":
            result: Any = repo.install_hooks()
        elif cmd == "pin":
            result = repo.git_pin()
        else:
            result = repo.git_pre_commit(progress=args.progress)
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    if cmd == "install-hooks":
        _log_hooks(result)
    elif cmd == "pin" and result.changed:
        logger.info(
            f"gitのコミット{result.pin.git[:12]}を版{result.pin.bvc}に対応付けました"
        )
    elif cmd == "pre-commit" and result.auto_commit is not None:
        logger.info(
            f"bvc: 未コミットの変更を版{result.auto_commit.id}に自動コミットしました"
        )
    return EXIT_OK


def _git_post_checkout(args: argparse.Namespace, start: Path) -> int:
    # sync に失敗しても git checkout は取り消せないので、目立つ警告にする(仕様書 5.3節)。
    # git が渡す引数(前後の HEAD、フラグ)は使わない。ファイル単位の checkout(フラグ 0)でも
    # bvc.lock が変わり得るので、常に作業ツリーの bvc.lock と現在位置の版の内容を比べて判断する。
    try:
        with Repo.open(start, warn_out_of_sync=False) as repo:
            result = repo.git_post_checkout(progress=args.progress)
    except BvcError as e:
        if args.json:
            raise
        bar = "=" * 60
        logger.error(
            f"{e}\n{bar}\nbvc syncに失敗しました。gitの作業ツリー(コード)とバイナリが食い違っています"
            f"(非同期状態)。\n原因を解消してからbvc syncを実行してください。\n{bar}",
            extra={"prefix": "中止" if isinstance(e, SafetyAbort) else None},
        )
        return e.exit_code
    if args.json:
        _print_json(args, result)
        return EXIT_OK
    if result.sync is not None:
        for line in format_sync(result.sync):
            logger.info(f"bvc: {line}")
    return EXIT_OK


def _log_hooks(r: HooksResult) -> None:
    if r.installed:
        logger.info(
            f"gitのフックを設置しました: {', '.join(r.installed)}({r.hooks_dir})"
        )
    if r.updated:
        logger.info(
            f"フックの作業フォルダの指定を、絶対パスから相対パスに直しました: {', '.join(r.updated)}"
        )
    if r.already:
        logger.info(f"設置済みのフック: {', '.join(r.already)}")
    for name, line in r.manual.items():
        logger.warning(
            f"既存のフック{Path(r.hooks_dir) / name}があるため設置していません。下記の処理を追加して下さい:\n  {line}"
        )


_COMMANDS: Final[dict[str, Any]] = {
    "init": _cmd_init,
    "commit": _cmd_commit,
    "log": _cmd_log,
    "undo": _cmd_move,
    "redo": _cmd_move,
    "goto": _cmd_move,
    "note": _cmd_note,
    "branch": _cmd_branch,
    "discard": _cmd_discard,
    "gc": _cmd_gc,
    "verify": _cmd_verify,
    "sync": _cmd_sync,
    "git": _cmd_git,
}


# ---------------------------------------------------------------------------
# 表示の整形
# ---------------------------------------------------------------------------


def format_move(r: MoveResult) -> list[str]:
    if not r.changed:
        return [f"変更なし(現在位置は版{r.after.at}です)"]
    if r.after.at == r.before.at:
        return [f"版{r.after.at}のまま、現在のブランチを切り替えました"]
    lines = [f"版{r.after.at}に移動しました"]
    if r.skipped:
        lines.append(f"  壊れた版 {', '.join(map(str, r.skipped))}を飛ばしました")
    if r.auto_commit is not None:
        lines.append(
            f"  未コミットの変更を版{r.auto_commit.id}に自動コミットしました({r.auto_commit.message})"
        )
    lines += [f"  restored: {p}" for p in r.restored]
    lines += [f"  deleted:  {p}" for p in r.deleted]
    return lines


def format_sync(r: SyncResult) -> list[str]:
    if not r.lock_found:
        return []  # 警告は repo 層が出している
    if not r.changed:
        return [f"変更なし(bvc.lockは現在位置の版{r.after.at}と同じ内容です)"]
    lines = format_move(r)
    if r.imported is not None:
        lines.insert(1, f"  bvc.lockの内容から版{r.imported.id}を作成しました(import)")
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
        lines.append(
            f"{mark} {n}{' ' * (w - _width(n))}  先端: {_or_none(b.tip)}  分岐元: {_or_none(b.fork)}"
        )
    return "\n".join(lines)


def format_discard(r: DiscardResult) -> list[str]:
    lines = [f"版{r.discarded}に削除の印を付けました"]
    if r.after.at != r.before.at:
        lines.append(f"  版{r.after.at}に移動しました")
        if r.auto_commit is not None:
            lines.append(
                f"  未コミットの変更を版{r.auto_commit.id}に自動コミットしました({r.auto_commit.message})"
            )
        lines += [f"  restored: {p}" for p in r.restored]
        lines += [f"  deleted:  {p}" for p in r.deleted]
    return lines


def format_gc(r: GcReport) -> str:
    total = (
        len(r.deleted_commits) + r.deleted_manifests + r.deleted_chunks + r.deleted_tmp
    )
    if total == 0:
        return "削除対象がありません"
    versions = ", ".join(map(str, r.deleted_commits)) or "なし"
    detail = (
        f"版{versions}、マニフェスト{r.deleted_manifests}、チャンク{r.deleted_chunks}、"
        f"一時ファイル{r.deleted_tmp}(合計{format_size(r.freed_bytes)})"
    )
    return f"削除対象: {detail}" if r.dry_run else f"{detail}を削除しました"


def format_verify(r: VerifyReport) -> list[str]:
    # 最後の行が結果(異常が残っていれば、壊れた版の一覧)。
    lines = [
        (
            f"チャンク{r.checked_chunks}、"
            f"マニフェスト{r.checked_manifests}、版{r.checked_commits}を検査しました{'(--quick)' if r.quick else ''}"
        )
    ]
    if r.repaired_chunks or r.repaired_manifests:
        lines.append(
            f"  チャンク{len(r.repaired_chunks)}、マニフェスト{len(r.repaired_manifests)}を修復しました"
        )
    if r.bad_chunks or r.bad_manifests:
        lines.append(
            f"  チャンク{len(r.bad_chunks)}、マニフェスト{len(r.bad_manifests)}は欠損/破損しています"
        )
    if r.ok:
        lines.append("異常はありません")
    else:
        hint = "" if r.repair else "(verify --repairで修復できる場合があります)"
        broken = "\n".join(map(str, r.broken_commits))
        lines.append(f"以下の版は壊れています{hint}\n{broken}")
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
        return datetime.fromisoformat(iso).strftime("%Y/%m/%d %H:%M")
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
