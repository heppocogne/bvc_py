# bvc: バイナリファイル向けバージョン管理システム(試作)

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _get_version() -> str:
    try:
        return version("bvc_py")
    except PackageNotFoundError:
        pass

    # 未インストールでソースから実行している場合(開発時)。.pyz はビルド時にこのファイルを置き換える
    try:
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


__version__ = _get_version()
__all__ = ["__version__"]
