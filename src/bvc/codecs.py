# チャンクの圧縮方式(codec)とレジストリ。設計書 2.2節・3.3節。
# チャンクファイルは [1バイト: codec ID][ペイロード]。

from __future__ import annotations

import zlib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import ClassVar, Final

from bvc.errors import CorruptData, UnsupportedFormat

# 逐次復号で一度に取り出す最大のバイト数
DECODE_BLOCK: Final[int] = 16 << 20
# auto で試しに圧縮する先頭部分の大きさと、raw を選ぶ圧縮率のしきい値
AUTO_SAMPLE: Final[int] = 256 << 10
AUTO_RATIO: Final[float] = 0.95
ZLIB_LEVEL: Final[int] = 1

POLICIES: Final[tuple[str, ...]] = ("auto", "none", "zlib")


class Encoder(ABC):
    @abstractmethod
    def update(self, data: bytes) -> bytes: ...

    @abstractmethod
    def finish(self) -> bytes: ...


class Codec(ABC):
    id: ClassVar[int]
    name: ClassVar[str]

    @abstractmethod
    def encode(self, data: bytes) -> bytes: ...

    @abstractmethod
    def encoder(self) -> Encoder:
        # 逐次圧縮用の Encoder を返す(大きなチャンク用)。
        pass

    @abstractmethod
    def iter_decode(self, pieces: Iterable[bytes], limit: int) -> Iterator[bytes]:
        # ペイロードの断片を逐次復号する。
        # 復号後の合計が limit を超えそうになった時点で CorruptData を送出する
        # (改ざんされたデータでメモリを使い果たさないため。C-9)。
        pass

    def decode(self, data: bytes, limit: int) -> bytes:
        return b"".join(self.iter_decode((data,), limit))


class _RawEncoder(Encoder):
    def update(self, data: bytes) -> bytes:
        return data

    def finish(self) -> bytes:
        return b""


class RawCodec(Codec):
    id: ClassVar[int] = 0
    name: ClassVar[str] = "raw"

    def encode(self, data: bytes) -> bytes:
        return data

    def encoder(self) -> Encoder:
        return _RawEncoder()

    def iter_decode(self, pieces: Iterable[bytes], limit: int) -> Iterator[bytes]:
        total = 0
        for piece in pieces:
            total += len(piece)
            if total > limit:
                raise CorruptData("チャンクを復号したサイズが記録を超えています")
            if piece:
                yield piece


class _ZlibEncoder(Encoder):
    def __init__(self) -> None:
        self._c = zlib.compressobj(ZLIB_LEVEL)

    def update(self, data: bytes) -> bytes:
        return self._c.compress(data)

    def finish(self) -> bytes:
        return self._c.flush()


class ZlibCodec(Codec):
    id: ClassVar[int] = 1
    name: ClassVar[str] = "zlib"

    def encode(self, data: bytes) -> bytes:
        return zlib.compress(data, ZLIB_LEVEL)

    def encoder(self) -> Encoder:
        return _ZlibEncoder()

    def iter_decode(self, pieces: Iterable[bytes], limit: int) -> Iterator[bytes]:
        d = zlib.decompressobj()
        total = 0
        try:
            for piece in pieces:
                if d.eof:
                    if piece:
                        raise CorruptData("圧縮データの後ろに余分なデータがあります")
                    continue
                buf = piece
                while buf and not d.eof:
                    # 上限 +1 バイトまでしか取り出さない(超えたら破損と判断できる)
                    out = d.decompress(buf, min(DECODE_BLOCK, limit - total + 1))
                    total += len(out)
                    if total > limit:
                        raise CorruptData(
                            "チャンクを復号したサイズが記録を超えています"
                        )
                    if out:
                        yield out
                    buf = d.unconsumed_tail
                if d.eof and (buf or d.unused_data):
                    raise CorruptData("圧縮データの後ろに余分なデータがあります")
            # 入力を使い切っても、取り出し上限のために出力が残っていることがある
            while not d.eof:
                out = d.decompress(b"", min(DECODE_BLOCK, limit - total + 1))
                if not out:
                    break
                total += len(out)
                if total > limit:
                    raise CorruptData("チャンクを復号したサイズが記録を超えています")
                yield out
        except zlib.error as e:
            raise CorruptData(f"圧縮データを復号できません: {e}") from e
        if not d.eof:
            raise CorruptData("圧縮データが途中で途切れています")


_CODECS: Final[tuple[Codec, ...]] = (RawCodec(), ZlibCodec())
CODECS_BY_ID: Final[dict[int, Codec]] = {c.id: c for c in _CODECS}
CODECS_BY_NAME: Final[dict[str, Codec]] = {c.name: c for c in _CODECS}


def get_codec(codec_id: int) -> Codec:
    # codec ID から Codec を得る。知らない ID なら UnsupportedFormat(V-2)。
    try:
        return CODECS_BY_ID[codec_id]
    except KeyError:
        raise UnsupportedFormat(
            f"対応していない圧縮方式です(codec ID={codec_id})。新しい版の bvc で作られた可能性があります"
        ) from None


def choose_codec(policy: str, data: bytes) -> Codec:
    # 圧縮の方針から Codec を選ぶ。
    # "none" → raw、"zlib" → zlib、
    # "auto" → 先頭 256KiB を zlib L1 で試し、圧縮率が 0.95 を超えれば raw(圧縮が効かない)。
    if policy == "none":
        return CODECS_BY_NAME["raw"]
    if policy == "zlib":
        return CODECS_BY_NAME["zlib"]
    if policy == "auto":
        sample = data[:AUTO_SAMPLE]
        if not sample:
            return CODECS_BY_NAME["raw"]
        ratio = len(zlib.compress(sample, ZLIB_LEVEL)) / len(sample)
        return CODECS_BY_NAME["raw" if ratio > AUTO_RATIO else "zlib"]
    raise ValueError(f"不明な圧縮方式: {policy!r}")
