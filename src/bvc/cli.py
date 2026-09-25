# 引数解析・表示整形・終了コード・--json(設計書 1.1節)。
# M0 では共通オプションと --help / --version だけの仮実装。サブコマンドは M2 以降で追加する。

from __future__ import annotations

import argparse
import sys

from bvc import __version__

# 終了コード(仕様書 2.2節)。M1 で errors.py の exit_code と対応させる。
EXIT_OK = 0
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bvc",
        description="大容量バイナリファイル向けのローカル専用バージョン管理ツール",
    )
    parser.add_argument(
        "-C",
        dest="workdir",
        metavar="<パス>",
        help="作業フォルダを指定する(省略時はカレントフォルダから上へ向かって .bvc を探す)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="結果を JSON で出力する(GUI・スクリプト向け)",
    )
    parser.add_argument(
        "-q",
        dest="quiet",
        action="store_true",
        help="通常の出力を抑制する",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_subparsers(dest="command", metavar="<コマンド>", title="コマンド")
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse は --help / --version で 0、引数の誤りで 2 を渡して SystemExit を送出する
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    if args.command is None:
        parser.print_usage(sys.stderr)
        print("bvc: エラー: コマンドを指定してください", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK
