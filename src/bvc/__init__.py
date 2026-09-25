from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bvc_py")
except PackageNotFoundError:
    __version__ = "unknown"
