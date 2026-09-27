# 結合テストの共通フィクスチャ(実装計画書 4.2節・6.3節)。

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from tests import helpers

DRIVER: Final[Path] = Path(__file__).with_name("kill_driver.py")
# 待機に入らないまま止まった場合の保険(秒)
TIMEOUT: Final[int] = 60


@pytest.fixture
def workdir() -> Path:
    path = helpers.make_temp_dir()
    yield path
    helpers.remove_tree(path)


def run_driver(
    workdir: Path, pattern: str, count: int, op: str, *args: str
) -> list[str]:
    # ドライバを起動し、段階名 pattern の count 回目で kill する。出力された段階名の一覧を返す。
    # 段階に達しないまま終了した場合は AssertionError。kill の後は、残ったロックファイルを
    # 利用者の手順(案内に従って削除する)に倣って消す。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), str(workdir), pattern, str(count), op, *args],
        stdin=subprocess.DEVNULL,  # pytest の標準入力の差し替えで、Windows ではハンドルが無効になるため
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
    )
    timer = threading.Timer(TIMEOUT, proc.kill)
    timer.start()
    lines: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if line.startswith("WAIT "):
                proc.kill()
                break
    finally:
        timer.cancel()
        proc.wait()
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
    assert lines and lines[-1].startswith("WAIT "), (
        f"段階 {pattern}#{count} に達しませんでした(終了コード {proc.returncode})\n"
        + "\n".join(lines[-20:])
        + "\n"
        + err
    )
    lock = workdir / ".bvc" / "lock"
    assert lock.exists(), "kill したのにロックが残っていない"
    lock.unlink()
    return lines


@pytest.fixture
def driver() -> Callable[..., list[str]]:
    return run_driver
