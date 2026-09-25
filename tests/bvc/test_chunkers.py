# chunkers: fixed / whole の分割と params の検査(M1-10)。

import io
import unittest
from unittest import mock

from bvc import chunkers
from tests import helpers


def chunks_of(chunker, data):
    # split の結果をチャンク単位にまとめる。断片の大きさも確認する。
    result, cur = [], []
    for piece, end in chunker.split(io.BytesIO(data)):
        assert 0 < len(piece) <= chunkers.READ_SIZE, len(piece)
        cur.append(piece)
        if end:
            result.append(b"".join(cur))
            cur = []
    assert not cur, "終端の無いチャンクが残った"
    return result


class TestFixed(unittest.TestCase):
    def test_boundaries(self):
        # S-1: 0バイト、1バイト、チャンクサイズちょうど・±1
        size = 1000
        ck = chunkers.FixedChunker(size)
        for n in (0, 1, size - 1, size, size + 1, 2 * size, 3 * size + 1):
            with self.subTest(n=n):
                data = helpers.random_bytes(n, n)
                parts = chunks_of(ck, data)
                self.assertEqual(b"".join(parts), data)
                self.assertEqual([len(p) for p in parts[:-1]], [size] * (len(parts) - 1))
                self.assertEqual(len(parts), -(-n // size))

    def test_chunk_larger_than_read_size(self):
        # チャンクが読み込み単位より大きいと、1チャンクが複数の断片で返る
        with mock.patch.object(chunkers, "READ_SIZE", 300):
            ck = chunkers.FixedChunker(1000)
            data = helpers.random_bytes(2500, 1)
            pieces = list(ck.split(io.BytesIO(data)))
            self.assertEqual([len(p) for p, _ in pieces], [300, 300, 300, 100] * 2 + [300, 200])
            self.assertEqual([e for _, e in pieces].count(True), 3)
            self.assertEqual(chunks_of(ck, data), [data[:1000], data[1000:2000], data[2000:]])

    def test_short_reads(self):
        # read が要求より少なく返しても正しく分割する
        class Slow(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n, 7) if n and n > 0 else n)

        ck = chunkers.FixedChunker(100)
        data = helpers.random_bytes(1234, 5)
        parts = []
        cur = b""
        for piece, end in ck.split(Slow(data)):
            cur += piece
            if end:
                parts.append(cur)
                cur = b""
        self.assertEqual(parts, [data[i:i + 100] for i in range(0, 1234, 100)])

    def test_params(self):
        self.assertEqual(chunkers.FixedChunker(4096).params(), {"name": "fixed", "size": 4096})


class TestWhole(unittest.TestCase):
    def test_one_chunk(self):
        ck = chunkers.WholeChunker()
        with mock.patch.object(chunkers, "READ_SIZE", 100):
            for n in (1, 99, 100, 101, 1000):
                data = helpers.random_bytes(n, n)
                self.assertEqual(chunks_of(ck, data), [data])
        self.assertEqual(chunks_of(ck, b""), [])
        self.assertEqual(ck.params(), {"name": "whole"})
        self.assertTrue(ck.always_stream)


class TestMakeChunker(unittest.TestCase):
    def test_ok(self):
        self.assertIsInstance(chunkers.make_chunker({"name": "fixed", "size": 10}), chunkers.FixedChunker)
        self.assertIsInstance(chunkers.make_chunker({"name": "whole"}), chunkers.WholeChunker)

    def test_bad(self):
        bad = [
            None, [], {}, {"name": 1}, {"name": "gearx"}, {"name": "fixed"},
            {"name": "fixed", "size": 0}, {"name": "fixed", "size": -1},
            {"name": "fixed", "size": 1.5}, {"name": "fixed", "size": "10"},
            {"name": "fixed", "size": True}, {"name": "fixed", "size": 10, "extra": 1},
            {"name": "fixed", "size": chunkers.MAX_FIXED_SIZE + 1},
            {"name": "whole", "size": 10},
        ]
        for spec in bad:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    chunkers.make_chunker(spec)


if __name__ == "__main__":
    unittest.main()
