# 引数解析・表示整形・終了コード・--json。設計書 1.1節・3.7節。

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Final

from . import __version__
from .errors import BvcError
from .model import CommitResult
from .repo import Repo

EXIT_OK: Final[int] = 0
EXIT_USAGE: Final[int] = 2


def build_parser() -> argparse.ArgumentParser:
    # argparse を構成(M2-11)。

    parser = argparse.ArgumentParser(
        prog="bvc",
        description="大容量バイナリファイル向けのローカル専用バージョン管理ツール",
        add_help=False,
    )

    # グローバルオプション
    parser.add_argument(
        "-C",
        dest="workdir",
        metavar="<パス>",
        type=Path,
        default=Path.cwd(),
        help="作業フォルダを指定する(省略時はカレントフォルダ)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="結果を JSON で出力する",
    )
    parser.add_argument(
        "-q",
        dest="quiet",
        action="store_true",
        help="出力を抑制する",
    )
    parser.add_argument(
        "--help",
        action="store_true",
        help="ヘルプを表示",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="バージョンを表示",
    )

    # サブコマンド
    subparsers = parser.add_subparsers(dest="command", metavar="<コマンド>", title="コマンド")

    # init コマンド
    init_parser = subparsers.add_parser("init", help="リポジトリを初期化")
    init_parser.add_argument("path", nargs="?", default=".", help="作業フォルダ")
    init_parser.add_argument(
        "--track",
        nargs="+",
        default=["*"],
        help="追跡パターン(既定: *)",
    )
    init_parser.add_argument(
        "--ignore",
        nargs="*",
        default=[],
        help="除外パターン",
    )

    # commit コマンド
    commit_parser = subparsers.add_parser("commit", help="コミット")
    commit_parser.add_argument(
        "-m",
        "--message",
        default="",
        help="コミットメッセージ",
    )
    commit_parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="欠落ファイルを許可",
    )

    # log コマンド
    log_parser = subparsers.add_parser("log", help="履歴を表示")
    log_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        help="表示件数",
    )
    log_parser.add_argument(
        "--all",
        action="store_true",
        help="削除済みも含める",
    )

    return parser


def run(argv: list[str] | None = None) -> int:
    # エントリポイント(M2-11)。

    parser = build_parser()

    try:
        # argparse をテスト
        parsed = parser.parse_args(argv)

        # グローバルオプション
        if parsed.help and not parsed.command:
            parser.print_help()
            return EXIT_OK

        if parsed.version:
            print(f"bvc {__version__}")
            return EXIT_OK

        # コマンド実行
        if parsed.command == "init":
            result = _cmd_init(parsed)
        elif parsed.command == "commit":
            result = _cmd_commit(parsed)
        elif parsed.command == "log":
            result = _cmd_log(parsed)
        elif not parsed.command:
            parser.print_usage(sys.stderr)
            print("bvc: エラー: コマンドを指定してください", file=sys.stderr)
            return EXIT_USAGE
        else:
            print(f"不明なコマンド: {parsed.command}", file=sys.stderr)
            return EXIT_USAGE

        # 結果を出力
        if parsed.json:
            _output_json(result)
        else:
            _output_text(result)

        # 終了コード
        if isinstance(result, CommitResult) and not result.changed:
            return EXIT_OK
        elif isinstance(result, dict) and "error" in result:
            return result.get("exit_code", 1)

        return EXIT_OK

    except BvcError as e:
        parsed = parser.parse_args(argv)
        if parsed.json:
            error_data = {
                "error": str(e),
                "type": e.__class__.__name__,
                "exit_code": e.exit_code,
            }
            print(json.dumps(error_data, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"エラー: {e}", file=sys.stderr)

        return getattr(e, "exit_code", 1)

    except KeyboardInterrupt:
        print("キャンセルされました", file=sys.stderr)
        return 3

    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE

    except Exception as e:
        print(f"予期しないエラー: {e}", file=sys.stderr)
        return 1


def _cmd_init(args) -> dict:
    # init コマンド(M2-10)。

    workdir = Path(args.path).resolve()

    repo = Repo.init(
        workdir=workdir,
        track=args.track,
        ignore=args.ignore,
    )
    repo.close()

    return {
        "type": "init",
        "path": str(workdir),
        "success": True,
    }


def _cmd_commit(args) -> CommitResult:
    # commit コマンド(M2-10)。

    repo = Repo.open(args.workdir)
    result = repo.commit(
        message=args.message,
        allow_missing=args.allow_missing,
    )
    repo.close()

    return result


def _cmd_log(args) -> dict:
    # log コマンド(M2-10)。

    repo = Repo.open(args.workdir)
    entries = repo.log(
        include_discarded=args.all,
        limit=args.limit,
    )
    repo.close()

    # ツリー表示(M2-12)
    output = _format_log_tree(entries)

    return {
        "type": "log",
        "entries": [_entry_to_dict(e) for e in entries],
        "text": output,
    }


def _format_log_tree(entries) -> str:
    # log のツリー表示(M2-12)。仕様書 3.3節。

    lines = []

    for entry in entries:
        commit = entry.commit

        # マーク
        if entry.is_current:
            mark = "●"
        elif entry.discarded:
            mark = "✗"
        else:
            mark = "○"

        # ブランチ名
        branch_part = f" [{entry.branch_label}]" if entry.branch_label else ""

        # メッセージ
        message = commit.message if commit else "(読込不可)"

        # 行を組み立て
        line = f"{mark} {commit.id if commit else '?'}:{branch_part} {message}"
        lines.append(line)

    return "\n".join(lines)


def _output_json(result: Any) -> None:
    # JSON で出力。

    if is_dataclass(result):
        data = asdict(result)
    elif isinstance(result, dict):
        data = result
    else:
        data = {"result": result}

    print(json.dumps(data, ensure_ascii=False, indent=2))


def _output_text(result: Any) -> None:
    # テキストで出力。

    if isinstance(result, CommitResult):
        if result.changed:
            print(f"コミット {result.commit.id} を作成しました")
        else:
            print("変更がありません")

    elif isinstance(result, dict):
        if "text" in result:
            print(result["text"])
        else:
            for key, value in result.items():
                if key != "text" and key != "entries":
                    print(f"{key}: {value}")


def _entry_to_dict(entry) -> dict:
    # LogEntry を辞書に変換(JSON 出力用)。

    commit_data = None
    if entry.commit:
        commit_data = {
            "id": entry.commit.id,
            "parent": entry.commit.parent,
            "branch": entry.commit.branch,
            "time": entry.commit.time,
            "kind": entry.commit.kind,
            "message": entry.commit.message,
        }

    return {
        "commit": commit_data,
        "effective_parent": entry.effective_parent,
        "branch_label": entry.branch_label,
        "is_tip": entry.is_tip,
        "is_current": entry.is_current,
        "discarded": entry.discarded,
        "pinned": entry.pinned,
    }
