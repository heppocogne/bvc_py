# worktree の単体テスト(M2-7〜M2-9)。観点: S-4, R-5, P-3, P-7, I-14。

import json
import os
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from bvc import fsutil, worktree
from bvc.errors import FileBusy, FileChanging, UnsafePath
from bvc.model import Config
from bvc.store import ObjectStore
from bvc.worktree import Worktree
from tests import helpers

OTHER_SHA = "b" * 64


class WorktreeTestCase(helpers.TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.bvc = self.tmp / ".bvc"
        (self.bvc / "tmp").mkdir(parents=True)
        self.store = ObjectStore(self.bvc)
        self.addCleanup(self.store.close)
        self.wt = self.make(["**"])
        patcher = mock.patch.object(worktree, "RETRY_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, track, ignore=(), rules=()):
        return Worktree(self.tmp, self.bvc, Config(track=list(track), ignore=list(ignore), rules=list(rules)), self.store)

    def write(self, rel, data=b"x", age=None):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if age is not None:
            t = time.time_ns() - int(age * 1e9)
            os.utime(p, ns=(t, t))
        return p

    def count_hashes(self, wt=None):
        # ファイルの読み込み回数を数える(保存あり・なしの両方)
        wt = wt or self.wt
        calls = []
        orig_hash, orig_put = self.store.hash_file, self.store.put_file

        def hash_file(f, *a, **k):
            calls.append(f.name)
            return orig_hash(f, *a, **k)

        def put_file(f, *a, **k):
            calls.append(f.name)
            return orig_put(f, *a, **k)

        p1 = mock.patch.object(self.store, "hash_file", hash_file)
        p2 = mock.patch.object(self.store, "put_file", put_file)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return calls


class TestScan(WorktreeTestCase):
    def test_patterns_and_bvc_excluded(self):
        self.write("a.bin")
        self.write("b.tmp")
        self.write("sub/c.bin")
        (self.bvc / "x.bin").write_bytes(b"x")
        wt = self.make(["**/*.bin", "*.tmp"], ignore=["*.tmp"])
        self.assertEqual(wt.scan(), ["a.bin", "sub/c.bin"])
        self.assertEqual(self.make(["**"]).scan(), ["a.bin", "b.tmp", "sub/c.bin"])

    def test_p3_links_are_not_followed(self):
        outside = helpers.make_temp_dir()
        self.addCleanup(helpers.remove_tree, outside)
        (outside / "secret.bin").write_bytes(b"s")
        made = helpers.try_symlink(outside, self.tmp / "link", target_is_directory=True)
        made |= helpers.try_junction(outside, self.tmp / "junc")
        made_file = helpers.try_symlink(outside / "secret.bin", self.tmp / "f.bin")
        if not (made or made_file):
            self.skipTest("リンクを作れない環境")
        self.write("real.bin")
        with self.assertLogs("bvc.worktree", "WARNING") if made_file else _nullcontext():
            self.assertEqual(self.wt.scan(), ["real.bin"])

    def test_p7_normalization_collision_is_error(self):
        nfc = unicodedata.normalize("NFC", "é.bin")
        nfd = unicodedata.normalize("NFD", "é.bin")
        self.write(nfc)
        self.write(nfd)
        if len(os.listdir(self.tmp)) < 3:  # .bvc と2つのファイル
            self.skipTest("正規化の異なる名前を別々に作れないファイルシステム")
        with self.assertRaises(UnsafePath):
            self.wt.scan()

    def test_nfd_name_is_recorded_as_nfc(self):
        nfd = unicodedata.normalize("NFD", "é.bin")
        self.write(nfd, b"v")
        st = self.wt.state({}, store_chunks=False)
        self.assertEqual(list(st.tree), [unicodedata.normalize("NFC", "é.bin")])


class TestState(WorktreeTestCase):
    def test_added_modified_missing_renamed(self):
        self.write("a", b"1")
        self.write("b", b"2")
        base = self.wt.state({}, store_chunks=True).tree
        self.write("a", b"changed")
        (self.tmp / "b").rename(self.tmp / "b2")
        self.write("c", b"3")
        self.write("gone-src", b"4")
        base2 = dict(base, gone=base["a"])  # base にだけある別名のファイル
        st = self.wt.state(base2, store_chunks=False)
        self.assertEqual(st.modified, ["a"])
        self.assertEqual(st.renamed, [("b", "b2", 1.0)])
        self.assertEqual(sorted(st.added), ["c", "gone-src"])
        self.assertEqual(st.missing, ["gone"])
        self.assertTrue(st.dirty)

    def test_s4_unchanged_files_are_not_read(self):
        self.write("a", b"1", age=10)
        self.write("b", b"2", age=10)
        st = self.wt.state({}, store_chunks=True)
        self.wt.update_index(st.tree, st.fs_time_ns)
        calls = self.count_hashes()
        st2 = self.wt.state(st.tree, store_chunks=True)
        self.assertEqual(calls, [])
        self.assertFalse(st2.dirty)

    def test_i14_recent_files_are_rehashed(self):
        # 走査の開始から2秒以内に更新されたファイルは、size・mtime が同じでも読み直す
        p = self.write("a", b"1111")
        st = self.wt.state({}, store_chunks=True)
        self.wt.update_index(st.tree, st.fs_time_ns)
        mtime = p.stat().st_mtime_ns
        p.write_bytes(b"2222")
        os.utime(p, ns=(mtime, mtime))
        st2 = self.wt.state(st.tree, store_chunks=False)
        self.assertEqual(st2.modified, ["a"])

    def test_i14_uses_recorded_fs_time(self):
        # 判定は index に記録した fs_time_ns で行う(今回の走査の時刻ではない)
        p = self.write("a", b"1", age=100)
        m = p.stat().st_mtime_ns
        index = {"format": 1, "fs_time_ns": m + 1_000_000_000,
                 "entries": {"a": {"size": 1, "mtime_ns": m, "manifest": OTHER_SHA}}}
        fsutil.atomic_write_json(self.bvc / "index.json", index, self.bvc / "tmp")
        st = self.wt.state({}, store_chunks=False)
        self.assertNotEqual(st.tree["a"], OTHER_SHA)

    def test_cached_entry_is_compared_with_base(self):
        # stat キャッシュを使ったファイルも base と比べる
        p = self.write("a", b"1", age=100)
        m = p.stat().st_mtime_ns
        index = {"format": 1, "fs_time_ns": time.time_ns(),
                 "entries": {"a": {"size": 1, "mtime_ns": m, "manifest": OTHER_SHA}}}
        fsutil.atomic_write_json(self.bvc / "index.json", index, self.bvc / "tmp")
        base = {"a": "c" * 64}
        st = self.wt.state(base, store_chunks=False)
        self.assertEqual(st.tree["a"], OTHER_SHA)
        self.assertEqual(st.modified, ["a"])

    def test_cached_manifest_missing_from_store_is_resaved(self):
        p = self.write("a", b"1", age=100)
        m = p.stat().st_mtime_ns
        index = {"format": 1, "fs_time_ns": time.time_ns(),
                 "entries": {"a": {"size": 1, "mtime_ns": m, "manifest": OTHER_SHA}}}
        fsutil.atomic_write_json(self.bvc / "index.json", index, self.bvc / "tmp")
        st = self.wt.state({}, store_chunks=True)
        self.assertNotEqual(st.tree["a"], OTHER_SHA)
        self.assertTrue(self.store.manifest_ok(st.tree["a"]))

    def test_broken_index_is_rebuilt(self):
        self.write("a", b"1")
        for content in (b"{", json.dumps({"format": 1, "fs_time_ns": 0, "entries": {"../a": {}}}).encode()):
            with self.subTest(content=content):
                (self.bvc / "index.json").write_bytes(content)
                with self.assertLogs("bvc.worktree", "WARNING"):
                    st = self.wt.state({}, store_chunks=False)
                self.assertEqual(list(st.tree), ["a"])

    def test_rules_choose_chunker(self):
        self.write("big/x.bin", b"z" * 100)
        self.write("y.bin", b"z" * 100)
        rules = [{"pattern": "big/*.bin", "chunker": {"name": "whole"}, "compression": "none"}]
        wt = self.make(["**"], rules=rules)
        st = wt.state({}, store_chunks=True)
        self.assertEqual(self.store.get_manifest(st.tree["big/x.bin"]).chunker["name"], "whole")
        self.assertEqual(self.store.get_manifest(st.tree["y.bin"]).chunker["name"], "fixed")
        self.assertEqual(st.total_bytes, 200)


class TestBusyFiles(WorktreeTestCase):
    def test_r5_file_being_written_aborts_after_retries(self):
        p = self.write("a", b"1")
        orig = self.store.hash_file
        calls = []

        def growing(f, *a, **k):
            calls.append(1)
            r = orig(f, *a, **k)
            with open(p, "ab") as g:  # 読み取り中に追記され続ける
                g.write(b"+")
            return r

        with mock.patch.object(self.store, "hash_file", growing), self.assertRaises(FileChanging):
            self.wt.state({}, store_chunks=False)
        self.assertEqual(len(calls), worktree.RETRY_ATTEMPTS)

    def test_r5_retry_succeeds_when_writer_stops(self):
        p = self.write("a", b"1")
        orig = self.store.hash_file
        calls = []

        def once(f, *a, **k):
            calls.append(1)
            r = orig(f, *a, **k)
            if len(calls) == 1:
                with open(p, "ab") as g:
                    g.write(b"+")
            return r

        with mock.patch.object(self.store, "hash_file", once):
            st = self.wt.state({}, store_chunks=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(st.total_bytes, 2)

    def test_file_that_cannot_be_opened_aborts(self):
        self.write("a", b"1")

        def busy_open(path, *a, **k):
            if ".bvc" in str(path):
                return open(path, *a, **k)
            raise PermissionError("使用中")

        with mock.patch("bvc.worktree.open", busy_open, create=True):
            with self.assertRaises(FileBusy):
                self.wt.state({}, store_chunks=False)


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
