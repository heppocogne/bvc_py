# ファイルの分割方式(chunker)とレジストリ。設計書 3.2節。

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, BinaryIO, ClassVar, Final

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
            raise ValueError(
                f"fixed の size は 1〜{MAX_FIXED_SIZE} の整数にしてください: {size!r}"
            )
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

    # Gear ハッシュは 64bit で、直近 64 バイトの内容だけで決まる(古いバイトは押し出される)。
    # このため、途中に挿入・削除があっても、その先で境界が元と揃う(再同期する)。
    _HASH_BITS: ClassVar[int] = 64

    def __init__(self, min: int, avg: int, max: int, seed: int) -> None:
        # min/avg/max はバイト単位。avg は 2 の累乗。seed は int(Gear テーブル生成用)。
        if type(avg) is not int or avg <= 0 or (avg & (avg - 1)) != 0:
            raise ValueError(
                f"gear の avg は 2 の累乗の正の整数にしてください: {avg!r}"
            )
        if avg.bit_length() - 1 > self._HASH_BITS:
            raise ValueError(f"gear の avg が大きすぎます(最大 2^64): {avg!r}")
        if type(min) is not int or not 1 <= min <= avg:
            raise ValueError(
                f"gear の min は 1 以上 avg 以下の整数にしてください: {min!r}"
            )
        if type(max) is not int or not avg <= max:
            raise ValueError(f"gear の max は avg 以上の整数にしてください: {max!r}")
        if type(seed) is not int:
            raise ValueError(f"gear の seed は整数にしてください: {seed!r}")
        self.min = min
        self.avg = avg
        self.max = max
        self.seed = seed
        # 判定に使うマスク: ハッシュの上位 log2(avg) ビットが全て 0 なら分割点。
        # 上位ビットは直近 64 バイト全てに依存する(下位ビットは直近数バイトにしか依存しない)。
        bits = avg.bit_length() - 1
        self._mask = ((1 << bits) - 1) << (self._HASH_BITS - bits)
        # seed から Gear テーブル生成
        rng = random.Random(seed)
        self._table = [rng.getrandbits(self._HASH_BITS) for _ in range(256)]

    def params(self) -> dict:
        return {
            "name": self.name,
            "min": self.min,
            "avg": self.avg,
            "max": self.max,
            "seed": self.seed,
        }

    def split(self, f: BinaryIO) -> Iterator[tuple[bytes, bool]]:
        yield from _finish_last(self._cut(f))

    def _cut(self, f: BinaryIO) -> Iterator[tuple[bytes, bool]]:
        # 分割点で True を付けて返す。ファイル末尾の未完のチャンクは False のまま返る
        # (呼び出し側の _finish_last が最後の断片を True にする)。
        table = self._table
        mask = self._mask
        hash_mask = (1 << self._HASH_BITS) - 1
        min_size = self.min
        max_size = self.max
        # min 未満では分割しないので、ハッシュは min の 64 バイト手前から計算すれば足りる
        hash_from = min_size - self._HASH_BITS if min_size > self._HASH_BITS else 0
        clen = 0  # 現在のチャンクの、pos までの長さ
        fp = 0
        for piece in _read_limited(f, None):
            n = len(piece)
            start = 0  # piece のうち、まだ返していない部分の先頭
            pos = 0
            while pos < n:
                if clen < hash_from:
                    step = min(n - pos, hash_from - clen)
                    pos += step
                    clen += step
                    continue
                limit = min(n, pos + (max_size - clen))
                cut = -1
                need = min_size - clen  # 分割してよい最小の位置(pos からの距離)
                for i in range(pos, limit):
                    fp = ((fp << 1) + table[piece[i]]) & hash_mask
                    if not fp & mask and i - pos + 1 >= need:
                        cut = i + 1
                        break
                if cut < 0:
                    clen += limit - pos
                    pos = limit
                    if clen >= max_size:
                        cut = pos
                else:
                    clen += cut - pos
                    pos = cut
                if cut >= 0:
                    yield piece[start:cut], True
                    start = pos
                    clen = 0
                    fp = 0
            if start < n:
                yield piece[start:], False


def _finish_last(
    parts: Iterator[tuple[bytes, bool]],
) -> Iterator[tuple[bytes, bool]]:
    # 最後の断片は、ファイル末尾なのでチャンク終端として返す(1つ先読みする)。
    prev = None
    for part in parts:
        if prev is not None:
            yield prev
        prev = part
    if prev is not None:
        yield prev[0], True


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
        raise ValueError(
            f"不明な分割方式です: {spec['name']!r}(使えるもの: {', '.join(CHUNKERS)})"
        )
    params = {k: v for k, v in spec.items() if k != "name"}
    try:
        return cls(**params)
    except TypeError as e:
        raise ValueError(
            f"{spec['name']} の設定項目が不正です: {sorted(params)}"
        ) from e
