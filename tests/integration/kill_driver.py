# 強制終了テスト用のドライバ(実装計画書 6.3節、I-10)。
#
# 使い方: python kill_driver.py <作業フォルダ> <段階名の正規表現> <回数> <操作> [引数...]
#   操作: commit [メッセージ] | undo | redo | goto <版> | gc | open(開いて recover するだけ)
#
# 障害注入のフック(fsutil._fault_hook)と進捗で段階名を1行ずつ標準出力に書く。
# 段階名が正規表現に一致した回数が <回数> に達したら "WAIT <段階名>" を書いて待機する。
# 親プロセス(テスト)はその行を読んだら、このプロセスを kill する。
# 本体にはテスト専用の分岐を入れない(このドライバが Repo を import して操作する)。

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bvc import fsutil
from bvc.repo import Repo
from tests import (
    helpers,  # noqa: F401  長いパスの設定(BVC_TEST_LONG_PATH)を反映する
)

WAIT_SECONDS: Final[int] = 600


def main(argv: list[str]) -> int:
    workdir, pattern, count_str, op, *args = argv
    pat = re.compile(pattern)
    count = int(count_str)
    seen = 0

    def hook(stage: str) -> None:
        nonlocal seen
        print(stage, flush=True)
        if pat.fullmatch(stage):
            seen += 1
            if seen == count:
                print(f"WAIT {stage}", flush=True)
                time.sleep(WAIT_SECONDS)

    def progress(ev) -> None:
        print(f"progress:{ev.stage}", flush=True)

    fsutil._fault_hook = hook
    with Repo.open(Path(workdir)) as repo:
        if op == "commit":
            repo.commit(args[0] if args else "", progress=progress)
        elif op == "undo":
            repo.undo(progress=progress)
        elif op == "redo":
            repo.redo(progress=progress)
        elif op == "goto":
            repo.goto(args[0], progress=progress)
        elif op == "gc":
            repo.gc(progress=progress)
        elif op != "open":
            raise SystemExit(f"不明な操作: {op}")
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
