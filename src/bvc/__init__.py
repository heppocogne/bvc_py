# bvc: バイナリファイル向けバージョン管理システム(試作)

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import tomllib


def _get_version() -> str:
    try:
        return version("bvc_py")
    except PackageNotFoundError:
        pass

    try:
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                return data.get("project", {}).get("version", "unknown")
    except Exception:
        pass

    return "unknown"


__version__ = _get_version()
__all__ = ["__version__"]
