# テスト用の共通処理(実装計画書 4.2節・6節)。
# 単体テスト(unittest 形式)から使うため、pytest を import しない。

from __future__ import annotations

import functools
import hashlib
import os
import random
import shutil
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final, TypeVar
from unittest import mock

ENV_RUN_SLOW: Final[str] = "BVC_RUN_SLOW"
ENV_LONG_PATH: Final[str] = "BVC_TEST_LONG_PATH"

_F = TypeVar("_F", bound=Callable)


# ---------------------------------------------------------------------------
# 長いパスの経路(I-16、実装計画書 6.5節)
# ---------------------------------------------------------------------------


def _apply_long_path_setting() -> None:
    # 環境変数 BVC_TEST_LONG_PATH があれば、fsutil.os_path が全パスを \\?\ 付きにするよう閾値を 0 にする。
    #
    # 本体は環境変数を読まない。fsutil の関数は LONG_PATH_THRESHOLD をモジュール変数として
    # 呼び出しのたびに参照すること(from import で値を写し取らない)。
    if not os.environ.get(ENV_LONG_PATH):
        return
    # fsutil は M1 で追加する。fsutil が無いのに指定された場合は、黙って通常の経路で
    # 実行しないよう ImportError のまま失敗させる。
    from bvc import fsutil

    fsutil.LONG_PATH_THRESHOLD = 0


_apply_long_path_setting()


# Windows の LongPathsEnabled が無効な環境の模擬(M4-12、P-6)。
# 監査フック(sys.addaudithook)で、\\?\ の付かない長いパスを OS に渡す操作を失敗させる。
# 監査イベントの無い操作(os.stat, os.lstat, os.path.realpath など)は検出できない。
# フックは外せないため、一度だけ登録してフラグで有効・無効を切り替える。
_MAX_PATH: Final[int] = 260
_MAX_DIR_PATH: Final[int] = 248  # CreateDirectoryW の上限(MAX_PATH - 12)
_PATH_ARGS: Final[dict[str, tuple[int, ...]]] = {
    "open": (0,),
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.rename": (0, 1),
    "os.scandir": (0,),
    "os.listdir": (0,),
    "os.chmod": (0,),
    "os.utime": (0,),
    "os.truncate": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.copyfile": (0, 1),
}
_no_long_paths = False
_long_path_hook_added = False


def _long_path_hook(event: str, args: tuple) -> None:
    if not _no_long_paths:
        return
    indexes = _PATH_ARGS.get(event)
    if indexes is None:
        return
    for i in indexes:
        if i >= len(args) or not isinstance(args[i], (str, bytes, os.PathLike)):
            continue  # ファイル記述子・None
        p = os.fsdecode(os.fspath(args[i]))
        if p.startswith("\\\\?\\"):
            continue
        limit = _MAX_DIR_PATH if event == "os.mkdir" else _MAX_PATH
        if len(os.path.abspath(p)) >= limit:
            raise FileNotFoundError(
                3, "長いパスは使えません(LongPathsEnabled 無効の模擬)", p
            )


class no_long_paths:
    # with 文の中だけ、LongPathsEnabled が無効な環境を模擬する(Windows のみ意味がある)。

    def __enter__(self) -> None:
        global _no_long_paths, _long_path_hook_added
        if not _long_path_hook_added:
            import sys

            sys.addaudithook(_long_path_hook)
            _long_path_hook_added = True
        _no_long_paths = True

    def __exit__(self, *exc: object) -> None:
        global _no_long_paths
        _no_long_paths = False


# ---------------------------------------------------------------------------
# slow(実装計画書 6.5節、I-15)
# ---------------------------------------------------------------------------


def run_slow_enabled() -> bool:
    return bool(os.environ.get(ENV_RUN_SLOW))


def slow(func: _F) -> _F:
    # 時間のかかるテストに付ける。環境変数 BVC_RUN_SLOW が無ければ skip する。
    #
    # 判定は import 時ではなく実行時に行う。pytest でノード ID を個別に指定したときは、
    # conftest がその項目の実行中だけ環境変数を設定する。

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not run_slow_enabled():
            raise unittest.SkipTest(
                f"時間のかかるテスト(実行するには {ENV_RUN_SLOW}=1 または pytest --run-slow)"
            )
        return func(*args, **kwargs)

    wrapper._bvc_slow = True  # type: ignore[attr-defined]  # conftest が slow マーカーを付ける目印
    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 一時フォルダ
# ---------------------------------------------------------------------------


class TempDirTestCase(unittest.TestCase):
    # テストごとに一時フォルダ self.tmp を作り、終了時に消す。
    # 利用者の presets.json を読まないよう、BVC_CONFIG_DIR を空の一時フォルダ self.config_dir にする。

    tmp: Path
    config_dir: Path

    def setUp(self) -> None:
        super().setUp()
        self.tmp = make_temp_dir()
        self.addCleanup(remove_tree, self.tmp)
        self.config_dir = make_temp_dir("bvc-config-")
        self.addCleanup(remove_tree, self.config_dir)
        patcher = mock.patch.dict(os.environ, {"BVC_CONFIG_DIR": str(self.config_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)


def make_temp_dir(prefix: str = "bvc-test-") -> Path:
    # realpath にしておく(macOS の /var → /private/var、Windows の 8.3 形式の短い名前など)
    return Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))


def remove_tree(path: Path) -> None:
    # 読み取り専用属性のファイルがあっても消す(P-8 のテストの後始末など)。

    def onerror(func, p, exc_info):
        os.chmod(p, 0o700)
        func(p)

    if path.exists():
        # onerror は 3.12 で非推奨だが、3.11 には onexc が無いため使う(I-12)
        shutil.rmtree(path, onerror=onerror)


# ---------------------------------------------------------------------------
# 疑似乱数ファイル
# ---------------------------------------------------------------------------


def random_bytes(size: int, seed: int = 0) -> bytes:
    # seed から決まる疑似乱数のバイト列(圧縮の効かない内容)。
    return random.Random(seed).randbytes(size)


def write_random_file(path: Path, size: int, seed: int = 0) -> str:
    # 疑似乱数の内容でファイルを作り、SHA-256 を返す。親フォルダも作る。
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    rng = random.Random(seed)
    block = 1 << 20
    with open(path, "wb") as f:
        remaining = size
        while remaining > 0:
            n = min(block, remaining)
            data = rng.randbytes(n)
            f.write(data)
            h.update(data)
            remaining -= n
    return h.hexdigest()


# ---------------------------------------------------------------------------
# SHA-256 とツリーの比較
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def iter_files(root: Path, exclude: tuple[str, ...] = (".bvc",)) -> Iterator[Path]:
    # root 以下のファイルを列挙する。exclude の名前のフォルダ(直下)は除く。
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d not in exclude]
        for name in filenames:
            yield Path(dirpath, name)


def tree_hashes(root: Path, exclude: tuple[str, ...] = (".bvc",)) -> dict[str, str]:
    # root 以下の全ファイルの {相対パス('/' 区切り): SHA-256}。
    return {
        p.relative_to(root).as_posix(): sha256_file(p)
        for p in iter_files(root, exclude)
    }


# ---------------------------------------------------------------------------
# 破損の再現(実装計画書 6.4節)
# ---------------------------------------------------------------------------


class FaultAt:
    # 障害注入のフック(実装計画書 6.2節)。fsutil._fault_hook に差し込む。
    #
    # 段階名が stage に一致したら、count 回目に exc を送出する。呼ばれた段階名を calls に記録する。
    # stage に None を渡すと、記録だけする。

    def __init__(
        self, stage: str | None = None, exc: BaseException | None = None, count: int = 1
    ):
        self.stage = stage
        self.exc = exc if exc is not None else OSError("注入した障害")
        self.count = count
        self.calls: list[str] = []
        self._seen = 0

    def __call__(self, stage: str) -> None:
        self.calls.append(stage)
        if stage == self.stage:
            self._seen += 1
            if self._seen == self.count:
                raise self.exc


def flip_byte(path: Path, offset: int = -1) -> None:
    # offset の1バイトを反転する(負数は末尾から)。
    data = bytearray(path.read_bytes())
    data[offset] ^= 0xFF
    path.write_bytes(bytes(data))


def truncate_file(path: Path, size: int) -> None:
    with open(path, "r+b") as f:
        f.truncate(size)


def set_first_byte(path: Path, value: int) -> None:
    # 先頭1バイト(チャンクの codec ID)を書き換える。
    data = bytearray(path.read_bytes())
    data[0] = value
    path.write_bytes(bytes(data))


def break_json(path: Path) -> None:
    # JSON として読めない内容にする。
    path.write_bytes(path.read_bytes()[:-3] + b"\x00{")


def try_symlink(target: Path, link: Path, target_is_directory: bool = False) -> bool:
    # シンボリックリンクを作る。権限などで作れなければ False。
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        return False
    return True


def try_junction(target: Path, link: Path) -> bool:
    # Windows のジャンクションを作る(Windows 以外・作れなければ False)。
    if os.name != "nt":
        return False
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError):
        return False
    return True
