# ファイルの分割方式(chunker)とレジストリ。設計書 3.2節。

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any, BinaryIO, ClassVar, Final, Iterator

# 読み込み単位(split が返す断片の最大サイズ)
READ_SIZE: Final[int] = 16 << 20
# fixed のチャンクサイズの上限(誤設定でメモリを使い果たさないための目安)
MAX_FIXED_SIZE: Final[int] = 1 << 30


class Chunker(ABC):
    name: ClassVar[str]
    # True なら、チャンクの大きさによらず store が逐次保存(put_chunk_stream)を使う
    always_stream: ClassVar[bool] = False

    @abstractmethod
    def params(self) -> dict:
        # マニフェストに記録する設定(name を含む)。
        pass

    @abstractmethod
    def split(self, f: BinaryIO) -> Iterator[tuple[bytes, bool]]:
        # (断片, チャンク終端か) を逐次返す。断片は最大 READ_SIZE。
        # チャンクが断片より大きくても、メモリに全体を載せずに済む。空ファイルでは何も返さない。
        pass


def _read_limited(f: BinaryIO, limit: int | None) -> Iterator[bytes]:
    # f から最大 limit バイト(None なら末尾まで)を READ_SIZE 以下の断片で返す。
    remaining = limit
    while remaining is None or remaining > 0:
        n = READ_SIZE if remaining is None else min(READ_SIZE, remaining)
        piece = f.read(n)
        if not piece:
            return
        if remaining is not None:
            remaining -= len(piece)
        yield piece


def _mark_last(pieces: Iterator[bytes]) -> Iterator[tuple[bytes, bool]]:
    # 最後の断片にだけ True を付ける(1つ先読みする)。
    prev = None
    for piece in pieces:
        if prev is not None:
            yield prev, False
        prev = piece
    if prev is not None:
        yield prev, True


class FixedChunker(Chunker):
    name: ClassVar[str] = "fixed"

    def __init__(self, size: int) -> None:
        if type(size) is not int or not 1 <= size <= MAX_FIXED_SIZE:
            raise ValueError(f"fixed の size は 1〜{MAX_FIXED_SIZE} の整数にしてください: {size!r}")
        self.size = size

    def params(self) -> dict:
        return {"name": self.name, "size": self.size}

    def split(self, f: BinaryIO) -> Iterator[tuple[bytes, bool]]:
        while True:
            got = 0
            for piece, end in _mark_last(_read_limited(f, self.size)):
                got += len(piece)
                yield piece, end
            if got < self.size:  # 末尾に達した
                return


class WholeChunker(Chunker):
    name: ClassVar[str] = "whole"
    always_stream: ClassVar[bool] = True

    def __init__(self) -> None:
        pass

    def params(self) -> dict:
        return {"name": self.name}

    def split(self, f: BinaryIO) -> Iterator[tuple[bytes, bool]]:
        yield from _mark_last(_read_limited(f, None))


class GearChunker(Chunker):
    name: ClassVar[str] = "gear"

    def __init__(self, min: int, avg: int, max: int, seed: int) -> None:
        # min/avg/max はバイト単位。avg は 2 の累乗。seed は int(Rabin fingerprint テーブル生成用)。
        if not isinstance(avg, int) or avg <= 0 or (avg & (avg - 1)) != 0:
            raise ValueError(f"gear の avg は 2 の累乗の正の整数にしてください: {avg!r}")
        if not isinstance(min, int) or not 1 <= min <= avg:
            raise ValueError(f"gear の min は 1 以上 avg 以下の整数にしてください: {min!r}")
        if not isinstance(max, int) or not avg <= max:
            raise ValueError(f"gear の max は avg 以上の整数にしてください: {max!r}")
        if not isinstance(seed, int):
            raise ValueError(f"gear の seed は整数にしてください: {seed!r}")
        self.min = min
        self.avg = avg
        self.max = max
        self.seed = seed
        # avg から gear_bits を決める(例: avg=4096 → gear_bits=12)
        self.gear_bits = (avg - 1).bit_length()
        self.threshold = 1 << self.gear_bits
        # seed から Rabin fingerprint テーブル生成
        rng = random.Random(seed)
        self._table = [rng.randint(0, 0xFFFFFFFF) for _ in range(256)]

    def params(self) -> dict:
        return {"name": self.name, "min": self.min, "avg": self.avg, "max": self.max, "seed": self.seed}

    def split(self, f: BinaryIO) -> Iterator[tuple[bytes, bool]]:
        # Rabin fingerprint ベースの rolling hash で CDC。
        buf = bytearray()
        fp = 0  # Rabin fingerprint
        for piece in _read_limited(f, None):
            for byte_val in piece:
                # rolling hash: (fp << 8) ^ table[shift-out byte] ^ new byte
                if buf:
                    fp = ((fp << 8) ^ self._table[(fp >> 24) & 0xFF]) & 0xFFFFFFFF
                fp = (fp ^ self._table[byte_val]) & 0xFFFFFFFF
                buf.append(byte_val)
                # avg バイト以降、分割ポイントをチェック
                if len(buf) >= self.avg and (fp & (self.threshold - 1)) == 0:
                    yield bytes(buf), False
                    buf.clear()
                    fp = 0
                # max に達したら強制分割
                elif len(buf) >= self.max:
                    yield bytes(buf), False
                    buf.clear()
                    fp = 0
        # 残り
        if buf:
            yield bytes(buf), True


CHUNKERS: Final[dict[str, type[Chunker]]] = {
    FixedChunker.name: FixedChunker,
    WholeChunker.name: WholeChunker,
    GearChunker.name: GearChunker,
}


def make_chunker(spec: Any) -> Chunker:
    # 設定値 {"name": ..., その他の params} から Chunker を作る。不正なら ValueError。
    if not isinstance(spec, dict) or type(spec.get("name")) is not str:
        raise ValueError(f"chunker の指定が不正です: {spec!r}")
    cls = CHUNKERS.get(spec["name"])
    if cls is None:
        raise ValueError(f"不明な分割方式です: {spec['name']!r}(使えるもの: {', '.join(CHUNKERS)})")
    params = {k: v for k, v in spec.items() if k != "name"}
    try:
        return cls(**params)
    except TypeError as e:
        raise ValueError(f"{spec['name']} の設定項目が不正です: {sorted(params)}") from e
