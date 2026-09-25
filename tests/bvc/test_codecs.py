# codecs: 往復、auto の判断、未知の codec ID、復号サイズの上限(M1-9)。

import tracemalloc
import unittest
import zlib

from bvc import codecs
from bvc.errors import CorruptData, UnsupportedFormat
from tests import helpers


class TestCodecs(unittest.TestCase):
    SIZES = (0, 1, 1000, (1 << 20) + 7)

    def test_roundtrip(self):
        for codec in codecs.CODECS_BY_ID.values():
            for size in self.SIZES:
                for data in (helpers.random_bytes(size, size), bytes(size)):
                    with self.subTest(codec=codec.name, size=size):
                        enc = codec.encode(data)
                        self.assertEqual(codec.decode(enc, len(data)), data)

    def test_stream_roundtrip(self):
        data = helpers.random_bytes(300_000, 1) + bytes(300_000)
        for codec in codecs.CODECS_BY_ID.values():
            with self.subTest(codec=codec.name):
                e = codec.encoder()
                enc = b"".join([e.update(data[i:i + 70_000]) for i in range(0, len(data), 70_000)])
                enc += e.finish()
                # 入力を細かく分けて渡しても復号できる
                pieces = [enc[i:i + 999] for i in range(0, len(enc), 999)]
                self.assertEqual(b"".join(codec.iter_decode(pieces, len(data))), data)

    def test_ids(self):
        self.assertEqual(codecs.CODECS_BY_NAME["raw"].id, 0)
        self.assertEqual(codecs.CODECS_BY_NAME["zlib"].id, 1)

    def test_unknown_id(self):
        # V-2
        for cid in (2, 255):
            with self.assertRaises(UnsupportedFormat):
                codecs.get_codec(cid)

    def test_choose_codec(self):
        rnd = helpers.random_bytes(300_000, 2)
        self.assertEqual(codecs.choose_codec("none", bytes(1000)).name, "raw")
        self.assertEqual(codecs.choose_codec("zlib", rnd).name, "zlib")
        self.assertEqual(codecs.choose_codec("auto", bytes(1000)).name, "zlib")
        self.assertEqual(codecs.choose_codec("auto", rnd).name, "raw")
        self.assertEqual(codecs.choose_codec("auto", b"").name, "raw")
        # 先頭 256KiB だけで判断する
        self.assertEqual(codecs.choose_codec("auto", rnd + bytes(10**6)).name, "raw")
        with self.assertRaises(ValueError):
            codecs.choose_codec("lzma", b"")

    def test_raw_limit(self):
        with self.assertRaises(CorruptData):
            codecs.CODECS_BY_NAME["raw"].decode(b"abc", 2)

    def test_zlib_corrupt(self):
        z = codecs.CODECS_BY_NAME["zlib"]
        enc = z.encode(helpers.random_bytes(10_000, 3))
        cases = {
            "truncated": enc[:-5],
            "trailing": enc + b"x",
            "garbage": b"not zlib data",
            "empty": b"",
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(CorruptData):
                    z.decode(data, 10_000)

    def test_zlib_bomb(self):
        # C-9: 展開すると巨大になるデータでも、上限を少し超えた時点で止まり、メモリを使い果たさない
        bomb = zlib.compress(bytes(200 << 20), 9)
        self.assertLess(len(bomb), 1 << 20)
        z = codecs.CODECS_BY_NAME["zlib"]
        tracemalloc.start()
        try:
            with self.assertRaises(CorruptData):
                for _ in z.iter_decode([bomb], 1 << 20):
                    pass
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 << 20)


if __name__ == "__main__":
    unittest.main()
