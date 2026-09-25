# dataclass(M1-2)。

import dataclasses
import unittest

from bvc.model import ChunkRef, Manifest, PutStats, StoreVerifyResult


class TestModel(unittest.TestCase):
    def test_frozen(self):
        ref = ChunkRef("a" * 64, 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ref.length = 2  # type: ignore[misc]
        m = Manifest(size=1, sha256="b" * 64, chunker={"name": "whole"}, chunks=(ref,))
        self.assertEqual(m.chunks[0], ChunkRef("a" * 64, 1))

    def test_put_stats_add(self):
        a = PutStats(1, 2, 3, 4, 5)
        a.add(PutStats(10, 20, 30, 40, 50))
        self.assertEqual(a, PutStats(11, 22, 33, 44, 55))

    def test_store_verify_result_ok(self):
        self.assertTrue(StoreVerifyResult().ok)
        self.assertFalse(StoreVerifyResult(bad_chunks=["x"]).ok)
        self.assertFalse(StoreVerifyResult(broken_manifests={"x": "y"}).ok)


if __name__ == "__main__":
    unittest.main()
