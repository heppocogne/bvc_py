# tests/helpers.py 自体の確認(M0-2)。

import os
import unittest
from unittest import mock

from tests import helpers


class TestHelpers(helpers.TempDirTestCase):
    def test_temp_dir_exists(self):
        self.assertTrue(self.tmp.is_dir())

    def test_remove_tree_readonly(self):
        d = helpers.make_temp_dir()
        f = d / "sub" / "ro.bin"
        helpers.write_random_file(f, 10)
        os.chmod(f, 0o400)
        helpers.remove_tree(d)
        self.assertFalse(d.exists())

    @unittest.skipUnless(os.name == "nt", "Windows 固有")
    def test_no_long_paths(self):
        # LongPathsEnabled 無効の模擬: \\?\ の無い長いパスだけ失敗させ、with の外では何もしない
        long_dir = self.tmp.joinpath(*(["d" * 60] * 5))
        prefixed = "\\\\?\\" + str(long_dir)
        with helpers.no_long_paths():
            with self.assertRaises(FileNotFoundError):
                os.makedirs(long_dir)
            os.makedirs(prefixed)
            with open(prefixed + "\\f.bin", "wb") as f:
                f.write(b"x")
            with self.assertRaises(FileNotFoundError):
                open(long_dir / "f.bin", "rb")
            with self.assertRaises(FileNotFoundError):
                os.listdir(long_dir)
            self.assertEqual(os.listdir(self.tmp), ["d" * 60])  # 短いパスはそのまま
        self.assertFalse(helpers._no_long_paths)  # with の外では無効

    def test_random_file_is_deterministic(self):
        a = helpers.write_random_file(self.tmp / "a.bin", (1 << 20) + 3, seed=1)
        b = helpers.write_random_file(self.tmp / "x" / "b.bin", (1 << 20) + 3, seed=1)
        c = helpers.write_random_file(self.tmp / "c.bin", (1 << 20) + 3, seed=2)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, helpers.sha256_file(self.tmp / "a.bin"))
        self.assertEqual(helpers.random_bytes(100, 5), helpers.random_bytes(100, 5))

    def test_empty_file(self):
        h = helpers.write_random_file(self.tmp / "empty", 0)
        self.assertEqual(h, helpers.sha256_bytes(b""))

    def test_tree_hashes_excludes_bvc(self):
        helpers.write_random_file(self.tmp / "a.bin", 5, seed=1)
        helpers.write_random_file(self.tmp / "d" / "b.bin", 5, seed=2)
        helpers.write_random_file(self.tmp / ".bvc" / "HEAD.json", 5, seed=3)
        helpers.write_random_file(self.tmp / "d" / ".bvc" / "x", 5, seed=4)  # 直下以外は除かない
        self.assertEqual(
            sorted(helpers.tree_hashes(self.tmp)), ["a.bin", "d/.bvc/x", "d/b.bin"]
        )

    def test_slow_skips_without_env(self):
        calls = []

        @helpers.slow
        def f():
            calls.append(1)

        self.assertTrue(f._bvc_slow)
        with mock.patch.dict(os.environ, {helpers.ENV_RUN_SLOW: ""}):
            with self.assertRaises(unittest.SkipTest):
                f()
        with mock.patch.dict(os.environ, {helpers.ENV_RUN_SLOW: "1"}):
            f()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
