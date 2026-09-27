# 配布用の bvc.pyz(zipapp)と bvc.cmd を作る(実装計画書 M5-4、設計書 1.2節)。
#
# 使い方: python tools/build_pyz.py [出力先の .pyz](省略時は dist/bvc.pyz)
#
# - pyproject.toml の version を bvc/__init__.py に書き込む(zipapp では importlib.metadata で取れないため)。
# - 圧縮は deflate にする(ZIP_ZSTANDARD などは古い Python で読めない)。対象の Python 3.11 以降で動かす。
# - 出力先と同じフォルダに bvc.cmd(Windows 用。python "%~dp0bvc.pyz" %*)を置く。

from __future__ import annotations

import shutil
import sys
import tomllib
import zipapp
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

PROJECT_DIR: Final[Path] = Path(__file__).resolve().parent.parent
SRC_PACKAGE: Final[Path] = PROJECT_DIR / "src" / "bvc"
DEFAULT_OUTPUT: Final[Path] = PROJECT_DIR / "dist" / "bvc.pyz"
# Linux/macOS で ./bvc.pyz として直接実行するときのインタプリタ
INTERPRETER: Final[str] = "/usr/bin/env python3"

INIT_TEMPLATE: Final[str] = """\
# bvc: バイナリファイル向けバージョン管理システム(試作)
# build_pyz.py が生成したファイル(zipapp 用。バージョンを埋め込む)

__version__ = "{version}"
__all__ = ["__version__"]
"""

# zipapp の入口。zipapp.create_archive(main=...) が作るものは main() の戻り値を終了コードにしないため、自前で置く
MAIN_TEMPLATE: Final[str] = """\
import sys

from bvc.main import main

sys.exit(main())
"""

# 終了コードをそのまま返す(PowerShell の $LASTEXITCODE、cmd の %ERRORLEVEL%)
CMD_TEMPLATE: Final[str] = """\
@echo off
python "%~dp0{pyz_name}" %*
exit /b %ERRORLEVEL%
"""


def read_version(pyproject: Path = PROJECT_DIR / "pyproject.toml") -> str:
    # pyproject.toml の [project] の version を返す。
    return tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]


def _ignore(directory: str, names: list[str]) -> list[str]:
    return [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]


def build(output: Path = DEFAULT_OUTPUT) -> tuple[Path, Path]:
    # .pyz と bvc.cmd を作り、それぞれのパスを返す。
    output = Path(output).resolve()
    version = read_version()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        shutil.copytree(SRC_PACKAGE, root / "bvc", ignore=_ignore)
        (root / "bvc" / "__init__.py").write_text(
            INIT_TEMPLATE.format(version=version), "utf-8", newline="\n"
        )
        (root / "__main__.py").write_text(MAIN_TEMPLATE, "utf-8", newline="\n")
        # 一時ファイルに作ってから置き換える(途中で失敗しても前の .pyz を壊さない)
        tmp_pyz = Path(tmp) / output.name
        zipapp.create_archive(root, tmp_pyz, interpreter=INTERPRETER, compressed=True)
        shutil.move(str(tmp_pyz), str(output))
    cmd = output.with_suffix(".cmd")
    cmd.write_text(CMD_TEMPLATE.format(pyz_name=output.name), "utf-8", newline="\r\n")
    return output, cmd


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("使い方: python tools/build_pyz.py [出力先の .pyz]", file=sys.stderr)
        return 2
    pyz, cmd = build(Path(argv[0]) if argv else DEFAULT_OUTPUT)
    print(f"バージョン {read_version()} の {pyz} と {cmd} を作成しました")
    print(f"  実行方法: python {pyz} --version")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
