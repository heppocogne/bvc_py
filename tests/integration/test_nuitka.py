# Nuitka で作った実行ファイル(bvc.exe など)の結合テスト(docs/build_nuitka.md)。観点: F-11, F-12 相当。
# tools/build_nuitka.py で一時フォルダに作り、子プロセスとして起動する。コンパイルに数分かかるため slow。

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from tests import helpers

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BUILD_SCRIPT: Final[Path] = ROOT / "tools" / "build_nuitka.py"

pytestmark = pytest.mark.slow


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_nuitka", BUILD_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_nuitka = _load_build_module()

# nuitka(と裏で使う C コンパイラ)が無い環境では組み立てられないため、まとめて skip する
_HAS_NUITKA = importlib.util.find_spec("nuitka") is not None


@pytest.fixture(scope="module")
def exe():
    if not _HAS_NUITKA:
        pytest.skip('nuitka が入っていない(pip install -e ".[build]")')
    path = helpers.make_temp_dir()
    built = build_nuitka.build(path / f"bvc{build_nuitka.EXE_SUFFIX}")
    yield built
    helpers.remove_tree(path)


@pytest.fixture
def workdir():
    path = helpers.make_temp_dir()
    yield path
    helpers.remove_tree(path)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_exe(exe: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(exe), *args],
        cwd=cwd,
        env=_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_runs(exe, workdir):
    r = run_exe(exe, workdir, "--version")
    assert (r.returncode, r.stdout) == (0, f"bvc {build_nuitka.read_version()}\n")


def test_basic_scenario(exe, workdir):
    # 基本シナリオ: init → commit(-m 付き)→ undo → redo → log、終了コード
    # commit の "-m" は、onefile の自己起動保護に誤検知されたことがあるため必ず通す
    (workdir / "a.bin").write_bytes(b"v0")
    r = run_exe(exe, workdir, "init", "--track", "*.bin")
    assert r.returncode == 0, r.stderr
    (workdir / "a.bin").write_bytes(b"v1")
    r = run_exe(exe, workdir, "commit", "-m", "変更")
    assert (r.returncode, r.stderr) == (0, "")
    assert "版1を作成しました" in r.stdout
    assert run_exe(exe, workdir, "-q", "undo").returncode == 0
    assert (workdir / "a.bin").read_bytes() == b"v0"
    assert run_exe(exe, workdir, "-q", "redo").returncode == 0
    assert (workdir / "a.bin").read_bytes() == b"v1"
    r = run_exe(exe, workdir, "--json", "log")
    data = json.loads(r.stdout)
    assert [e["id"] for e in data["entries"]] == [1, 0]
    assert data["entries"][0]["is_current"] is True

    assert run_exe(exe, workdir, "redo").returncode == 4
    assert run_exe(exe, workdir, "goto", "99").returncode == 1
