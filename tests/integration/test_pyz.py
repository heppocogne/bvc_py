# zipapp(bvc.pyz)と bvc.cmd の結合テスト(実装計画書 M5-4)。観点: F-11, F-12(zipapp からの起動)。
# tools/build_pyz.py で一時フォルダに作り、子プロセスとして起動する。

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Final

import pytest

from tests import helpers

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BUILD_SCRIPT: Final[Path] = ROOT / "tools" / "build_pyz.py"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_pyz", BUILD_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_pyz = _load_build_module()


@pytest.fixture(scope="module")
def dist():
    path = helpers.make_temp_dir()
    pyz, cmd = build_pyz.build(path / "bvc.pyz")
    yield pyz, cmd
    helpers.remove_tree(path)


@pytest.fixture
def workdir():
    path = helpers.make_temp_dir()
    yield path
    helpers.remove_tree(path)


def _env() -> dict[str, str]:
    # ソースの bvc を読み込まないように PYTHONPATH を外す(-I と合わせて使う。-I では PYTHONIOENCODING が効かないので -X utf8 も付ける)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_pyz(pyz: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(pyz), *args],
        cwd=cwd,
        env=_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_archive_contents(dist):
    pyz, cmd = dist
    with zipfile.ZipFile(pyz) as zf:
        names = zf.namelist()
        # Python 3.11 でも読める圧縮方式だけを使う
        assert {i.compress_type for i in zf.infolist()} <= {
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
        }
        init = zf.read("bvc/__init__.py").decode("utf-8")
    assert "__main__.py" in names and "bvc/cli.py" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert f'__version__ = "{build_pyz.read_version()}"' in init
    assert pyz.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    assert (
        cmd.read_bytes()
        == b'@echo off\r\npython "%~dp0bvc.pyz" %*\r\nexit /b %ERRORLEVEL%\r\n'
    )


def test_runs_code_in_archive(dist, workdir):
    pyz, _ = dist
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import bvc; print(bvc.__file__)",
            str(pyz),
        ],
        env=_env(),
        stdin=subprocess.DEVNULL,  # pytest の標準入力の差し替えで、Windows ではハンドルが無効になるため
        capture_output=True,
        text=True,
        check=False,
    )
    assert Path(r.stdout.strip()).parent.parent == pyz
    r = run_pyz(pyz, workdir, "--version")
    assert (r.returncode, r.stdout) == (0, f"bvc {build_pyz.read_version()}\n")


def test_basic_scenario(dist, workdir):
    # 基本シナリオ: init → commit → undo → redo → log、終了コード(F-12)
    pyz, _ = dist
    (workdir / "a.bin").write_bytes(b"v0")
    r = run_pyz(pyz, workdir, "init", "--track", "*.bin")
    assert r.returncode == 0, r.stderr
    (workdir / "a.bin").write_bytes(b"v1")
    r = run_pyz(pyz, workdir, "commit", "-m", "変更")
    assert (r.returncode, r.stderr) == (0, "")
    assert "版1を作成しました" in r.stdout
    assert run_pyz(pyz, workdir, "-q", "undo").returncode == 0
    assert (workdir / "a.bin").read_bytes() == b"v0"
    assert run_pyz(pyz, workdir, "-q", "redo").returncode == 0
    assert (workdir / "a.bin").read_bytes() == b"v1"
    r = run_pyz(pyz, workdir, "--json", "log")
    data = json.loads(r.stdout)
    assert [e["id"] for e in data["entries"]] == [1, 0]
    assert data["entries"][0]["is_current"] is True

    assert run_pyz(pyz, workdir, "redo").returncode == 4
    assert run_pyz(pyz, workdir, "goto", "99").returncode == 1
    assert run_pyz(pyz, workdir, "goto").returncode == 2
    (workdir / "a.bin").unlink()
    assert run_pyz(pyz, workdir, "commit").returncode == 3


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows のみ")
def test_bvc_cmd(dist, workdir):
    # bvc.cmd が引数と終了コードをそのまま渡す(python は PATH にあるものを使う)
    _, cmd = dist
    (workdir / "a b.bin").write_bytes(b"1")

    def run(*args):
        return subprocess.run(
            [str(cmd), *args],
            cwd=workdir,
            env=_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )

    r = run("--json", "init", "--track", "a b.bin")
    assert r.returncode == 0, r.stderr
    assert list(json.loads(r.stdout)["commit"]["tree"]) == ["a b.bin"]
    assert run("undo").returncode == 4
    assert run("goto", "99").returncode == 1
