# 配布用の実行ファイル(bvc.exe など)を Nuitka で作る(docs/build_nuitka.md)。
#
# 使い方: python tools/build_nuitka.py [出力先の実行ファイル](省略時は dist/bvc.exe)
# 事前に `pip install -e ".[build]"` で Nuitka を入れておくこと。
#
# - pyproject.toml の version を bvc/__init__.py に書き込む(コンパイル後は pyproject.toml を
#   読めないため。tools/build_pyz.py と同じ理由)。
# - --onefile で1ファイルの実行ファイルにする。
# - tools/build_pyz.py と同じく、一時フォルダに root/__main__.py + root/bvc/ を作って入口にする
#   (bvc/__main__.py は `python -m bvc` 専用のため使わない。I-13)。

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

# Windows 環境で日本語を出力するため、stdout/stderr を UTF-8 でラップする
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline=None)
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", newline=None)

PROJECT_DIR: Final[Path] = Path(__file__).resolve().parent.parent
SRC_PACKAGE: Final[Path] = PROJECT_DIR / "src" / "bvc"
EXE_SUFFIX: Final[str] = ".exe" if sys.platform == "win32" else ""
DEFAULT_OUTPUT: Final[Path] = PROJECT_DIR / "dist" / f"bvc{EXE_SUFFIX}"

INIT_TEMPLATE: Final[str] = """\
# bvc: バイナリファイル向けバージョン管理システム(試作)
# build_nuitka.py が生成したファイル(実行ファイル用。バージョンを埋め込む)

__version__ = "{version}"
__all__ = ["__version__"]
"""

# Nuitka の入口。bvc/__main__.py と同じ内容だが、root 直下(bvc/ の外)に置く
MAIN_TEMPLATE: Final[str] = """\
import sys

from bvc.main import main

sys.exit(main())
"""


def read_version(pyproject: Path = PROJECT_DIR / "pyproject.toml") -> str:
    # pyproject.toml の [project] の version を返す。
    return tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]


def _ignore(directory: str, names: list[str]) -> list[str]:
    ignored = [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]
    # root/__main__.py を入口にするため、bvc/__main__.py は不要(I-13)
    if Path(directory).name == "bvc" and "__main__.py" in names:
        ignored.append("__main__.py")
    return ignored


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    # 実行ファイルを作り、そのパスを返す。
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
        build_dir = Path(tmp) / "build"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "nuitka",
                "--onefile",
                "--assume-yes-for-downloads",
                # onefile の起動保護が "commit -m" の -m を「自分自身を -m 付きで
                # 再帰実行しようとした」と誤検知して止めてしまうため、この保護だけ外す
                "--no-deployment-flag=self-execution",
                f"--output-dir={build_dir}",
                f"--output-filename={output.name}",
                str(root / "__main__.py"),
            ],
            cwd=root,
            check=True,
        )
        # 一時フォルダから出力先へ移す(途中で失敗しても前の実行ファイルを壊さない)
        shutil.move(str(build_dir / output.name), str(output))
    return output


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print(
            "使い方: python tools/build_nuitka.py [出力先の実行ファイル]",
            file=sys.stderr,
        )
        return 2
    exe = build(Path(argv[0]) if argv else DEFAULT_OUTPUT)
    print(f"バージョン {read_version()} の {exe} を作成しました")
    print(f"  実行方法: {exe} --version")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
