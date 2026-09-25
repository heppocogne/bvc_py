# .pyz ファイルを生成し、バージョン情報をハードコード
# Python 3.11+ を想定

import shutil
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import tomllib


def get_version_from_pyproject() -> str:
    # pyproject.toml からバージョンを取得
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def create_init_with_hardcoded_version(version: str) -> str:
    # バージョンをハードコードした __init__.py を生成
    return f'''# bvc: バイナリファイル向けバージョン管理システム(試作)

__version__ = "{version}"
__all__ = ["__version__"]
'''


def build_pyz(output_path: Path | str = "dist/bvc.pyz") -> None:
    # .pyz ファイルを生成
    #
    # Args:
    #     output_path: 生成先パス (デフォルト: dist/bvc.pyz)

    output_path = Path(output_path)
    project_dir = Path(__file__).parent.parent
    src_dir = project_dir / "src"

    # バージョン取得
    version = get_version_from_pyproject()
    print(f"バージョン: {version}")

    # 出力ディレクトリ作成
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"出力先: {output_path}")

    with TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # src/bvc をコピー
        bvc_dir = tmpdir / "bvc"
        shutil.copytree(src_dir / "bvc", bvc_dir)

        # バージョンをハードコード
        init_path = bvc_dir / "__init__.py"
        init_path.write_text(
            create_init_with_hardcoded_version(version), encoding="utf-8"
        )
        print(f"✓ {init_path.name} にバージョンをハードコード")

        # __main__.py を生成（エントリポイント）
        main_py = tmpdir / "__main__.py"
        main_py.write_text(
            "from bvc.main import main\nimport sys\nsys.exit(main())\n",
            encoding="utf-8",
        )
        print("✓ __main__.py を生成")

        # .pyz を生成
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_ZSTANDARD) as zf:
            # __main__.py
            zf.write(main_py, "__main__.py")

            # bvc パッケージ全体
            for py_file in bvc_dir.rglob("*.py"):
                arcname = py_file.relative_to(tmpdir)
                zf.write(py_file, arcname)

        print(f"✓ .pyz ファイル生成完了: {output_path}")
        print(f"  実行方法: python {output_path} --version")


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        print("エラー: Python 3.11 以上が必要です", file=sys.stderr)
        sys.exit(1)

    output = "dist/bvc.pyz" if len(sys.argv) == 1 else sys.argv[1]
    build_pyz(output)
    print("\n✅ .pyz 生成完了！")
