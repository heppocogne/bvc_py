# chunkers: fixed / whole / gear の分割と params の検査(M1-10)。

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
                self.assertEqual(
                    [len(p) for p in parts[:-1]], [size] * (len(parts) - 1)
                )
                self.assertEqual(len(parts), -(-n // size))

    def test_chunk_larger_than_read_size(self):
        # チャンクが読み込み単位より大きいと、1チャンクが複数の断片で返る
        with mock.patch.object(chunkers, "READ_SIZE", 300):
            ck = chunkers.FixedChunker(1000)
            data = helpers.random_bytes(2500, 1)
            pieces = list(ck.split(io.BytesIO(data)))
            self.assertEqual(
                [len(p) for p, _ in pieces], [300, 300, 300, 100] * 2 + [300, 200]
            )
            self.assertEqual([e for _, e in pieces].count(True), 3)
            self.assertEqual(
                chunks_of(ck, data), [data[:1000], data[1000:2000], data[2000:]]
            )

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
        self.assertEqual(parts, [data[i : i + 100] for i in range(0, 1234, 100)])

    def test_params(self):
        self.assertEqual(
            chunkers.FixedChunker(4096).params(), {"name": "fixed", "size": 4096}
        )


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


class TestGear(unittest.TestCase):
    # S-1, S-6: gear(CDC)の分割。分割点の終端フラグ、min/max、挿入・書き換えに対する再同期。
    PARAMS = {"min": 64, "avg": 256, "max": 2048, "seed": 1234}

    def make(self, **kw):
        return chunkers.GearChunker(**{**self.PARAMS, **kw})

    def test_boundaries(self):
        ck = self.make()
        for n in (0, 1, 63, 64, 65, 255, 256, 2047, 2048, 2049, 10000, 100000):
            with self.subTest(n=n):
                data = helpers.random_bytes(n, n)
                parts = chunks_of(ck, data)
                self.assertEqual(b"".join(parts), data)
                self.assertTrue(all(parts))
                # 最後以外は min 以上 max 以下、最後は max 以下
                for p in parts[:-1]:
                    self.assertTrue(64 <= len(p) <= 2048, len(p))
                if parts:
                    self.assertLessEqual(len(parts[-1]), 2048)

    def test_many_chunks(self):
        # 分割点で終端を返す(全体を1チャンクにしない)。平均は min + avg 付近になる
        data = helpers.random_bytes(1 << 20, 3)
        parts = chunks_of(self.make(), data)
        self.assertGreater(len(parts), 1000)
        self.assertLess(len(parts), 5000)
        avg = len(data) / len(parts)
        self.assertTrue(200 < avg < 500, avg)

    def test_max_forces_cut(self):
        # 定数バイトの繰り返しは、ほとんどの値で分割点にならないので、max ごとに切れる
        ck = self.make()
        forced = 0
        for b in range(256):
            data = bytes([b]) * 10000
            parts = chunks_of(ck, data)
            self.assertEqual(b"".join(parts), data)
            if [len(p) for p in parts] == [2048] * 4 + [10000 - 2048 * 4]:
                forced += 1
        self.assertGreater(forced, 200)

    def test_read_size_independent(self):
        # 読み込み単位(断片の大きさ)によらず、同じ分割になる
        ck = self.make()
        data = helpers.random_bytes(50000, 4)
        expected = chunks_of(ck, data)
        for read_size in (1, 7, 100, 300, 4096):
            with (
                self.subTest(read_size=read_size),
                mock.patch.object(chunkers, "READ_SIZE", read_size),
            ):
                self.assertEqual(chunks_of(ck, data), expected)

    def test_short_reads(self):
        class Slow(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n, 7) if n and n > 0 else n)

        ck = self.make()
        data = helpers.random_bytes(20000, 5)
        parts, cur = [], b""
        for piece, end in ck.split(Slow(data)):
            cur += piece
            if end:
                parts.append(cur)
                cur = b""
        self.assertEqual(cur, b"")
        self.assertEqual(parts, chunks_of(ck, data))

    def test_deterministic_and_seed(self):
        data = helpers.random_bytes(50000, 6)
        self.assertEqual(
            chunks_of(self.make(), data), chunks_of(self.make(), data)
        )
        self.assertNotEqual(
            chunks_of(self.make(), data), chunks_of(self.make(seed=999), data)
        )

    def test_resync_after_insert(self):
        # 先頭・途中への挿入の後、境界が元と揃い、大半のチャンクが共有される
        ck = self.make()
        data = helpers.random_bytes(1 << 20, 7)
        base = chunks_of(ck, data)
        for at in (0, 1000, len(data) // 2):
            with self.subTest(at=at):
                changed = data[:at] + helpers.random_bytes(100, 8) + data[at:]
                parts = chunks_of(ck, changed)
                self.assertEqual(b"".join(parts), changed)
                shared = len(set(base) & set(parts))
                self.assertGreater(shared, len(base) * 0.95, (shared, len(base)))

    def test_resync_after_delete(self):
        ck = self.make()
        data = helpers.random_bytes(1 << 20, 9)
        base = chunks_of(ck, data)
        changed = data[:5000] + data[5100:]
        shared = len(set(base) & set(chunks_of(ck, changed)))
        self.assertGreater(shared, len(base) * 0.95, (shared, len(base)))

    def test_overwrite_one_byte(self):
        # 1バイトの書き換えで変わるチャンクは、ごく一部
        ck = self.make()
        data = bytearray(helpers.random_bytes(1 << 20, 10))
        base = chunks_of(ck, bytes(data))
        data[len(data) // 2] ^= 0xFF
        parts = chunks_of(ck, bytes(data))
        self.assertLessEqual(len(set(parts) - set(base)), 3)

    def test_params(self):
        self.assertEqual(
            self.make().params(),
            {"name": "gear", "min": 64, "avg": 256, "max": 2048, "seed": 1234},
        )
        self.assertFalse(self.make().always_stream)

    def test_make_chunker(self):
        self.assertIsInstance(
            chunkers.make_chunker({"name": "gear", **self.PARAMS}),
            chunkers.GearChunker,
        )

    def test_bad_params(self):
        bad = [
            {"avg": 0},
            {"avg": 300},
            {"avg": 256.0},
            {"avg": 1 << 65, "max": 1 << 66},
            {"min": 0},
            {"min": 257},
            {"min": True},
            {"max": 255},
            {"seed": "1"},
            {"seed": 1.5},
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.make(**kw)
        for missing in ("min", "avg", "max", "seed"):
            spec = {"name": "gear", **self.PARAMS}
            del spec[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                chunkers.make_chunker(spec)


class TestMakeChunker(unittest.TestCase):
    def test_ok(self):
        self.assertIsInstance(
            chunkers.make_chunker({"name": "fixed", "size": 10}), chunkers.FixedChunker
        )
        self.assertIsInstance(
            chunkers.make_chunker({"name": "whole"}), chunkers.WholeChunker
        )

    def test_bad(self):
        bad = [
            None,
            [],
            {},
            {"name": 1},
            {"name": "gearx"},
            {"name": "fixed"},
            {"name": "fixed", "size": 0},
            {"name": "fixed", "size": -1},
            {"name": "fixed", "size": 1.5},
            {"name": "fixed", "size": "10"},
            {"name": "fixed", "size": True},
            {"name": "fixed", "size": 10, "extra": 1},
            {"name": "fixed", "size": chunkers.MAX_FIXED_SIZE + 1},
            {"name": "whole", "size": 10},
        ]
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                chunkers.make_chunker(spec)


if __name__ == "__main__":
    unittest.main()
