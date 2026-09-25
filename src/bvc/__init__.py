# bvc: バイナリファイル向けバージョン管理システム(試作)

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bvc_py")
except PackageNotFoundError:  # 未インストール(ソース直実行・zipapp)時
    __version__ = "unknown"

__all__ = ["__version__"]
