# worktree の単体テスト(M2-7〜M2-9, M3-2〜M3-5)。観点: S-4, R-2, R-3, R-5, P-1, P-3, P-7, I-14。

import json
import os
import time
import unicodedata
import unittest
from typing import Final
from unittest import mock

from bvc import fsutil, worktree
from bvc.errors import (
    BrokenVersion,
    BvcError,
    CorruptData,
    FileBusy,
    FileChanging,
    UnsafePath,
    UnsupportedFormat,
)
from bvc.model import Config, Head
from bvc.store import ObjectStore
from bvc.worktree import Worktree
from tests import helpers

OTHER_SHA: Final[str] = "b" * 64


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
        return Worktree(
            self.tmp,
            self.bvc,
            Config(track=list(track), ignore=list(ignore), rules=list(rules)),
            self.store,
        )

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
        with (
            self.assertLogs("bvc.worktree", "WARNING") if made_file else _nullcontext()
        ):
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
        index = {
            "format": 1,
            "fs_time_ns": m + 1_000_000_000,
            "entries": {"a": {"size": 1, "mtime_ns": m, "manifest": OTHER_SHA}},
        }
        fsutil.atomic_write_json(self.bvc / "index.json", index, self.bvc / "tmp")
        st = self.wt.state({}, store_chunks=False)
        self.assertNotEqual(st.tree["a"], OTHER_SHA)

    def test_cached_entry_is_compared_with_base(self):
        # stat キャッシュを使ったファイルも base と比べる
        p = self.write("a", b"1", age=100)
        m = p.stat().st_mtime_ns
        index = {
            "format": 1,
            "fs_time_ns": time.time_ns(),
            "entries": {"a": {"size": 1, "mtime_ns": m, "manifest": OTHER_SHA}},
        }
        fsutil.atomic_write_json(self.bvc / "index.json", index, self.bvc / "tmp")
        base = {"a": "c" * 64}
        st = self.wt.state(base, store_chunks=False)
        self.assertEqual(st.tree["a"], OTHER_SHA)
        self.assertEqual(st.modified, ["a"])

    def test_cached_manifest_missing_from_store_is_resaved(self):
        p = self.write("a", b"1", age=100)
        m = p.stat().st_mtime_ns
        index = {
            "format": 1,
            "fs_time_ns": time.time_ns(),
            "entries": {"a": {"size": 1, "mtime_ns": m, "manifest": OTHER_SHA}},
        }
        fsutil.atomic_write_json(self.bvc / "index.json", index, self.bvc / "tmp")
        st = self.wt.state({}, store_chunks=True)
        self.assertNotEqual(st.tree["a"], OTHER_SHA)
        self.assertTrue(self.store.manifest_ok(st.tree["a"]))

    def test_broken_index_is_rebuilt(self):
        self.write("a", b"1")
        for content in (
            b"{",
            json.dumps(
                {"format": 1, "fs_time_ns": 0, "entries": {"../a": {}}}
            ).encode(),
        ):
            with self.subTest(content=content):
                (self.bvc / "index.json").write_bytes(content)
                with self.assertLogs("bvc.worktree", "WARNING"):
                    st = self.wt.state({}, store_chunks=False)
                self.assertEqual(list(st.tree), ["a"])

    def test_rules_choose_chunker(self):
        self.write("big/x.bin", b"z" * 100)
        self.write("y.bin", b"z" * 100)
        rules = [
            {
                "pattern": "big/*.bin",
                "chunker": {"name": "whole"},
                "compression": "none",
            }
        ]
        wt = self.make(["**"], rules=rules)
        st = wt.state({}, store_chunks=True)
        self.assertEqual(
            self.store.get_manifest(st.tree["big/x.bin"]).chunker["name"], "whole"
        )
        self.assertEqual(
            self.store.get_manifest(st.tree["y.bin"]).chunker["name"], "fixed"
        )
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

        with (
            mock.patch.object(self.store, "hash_file", growing),
            self.assertRaises(FileChanging),
        ):
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

        with (
            mock.patch("bvc.worktree.open", busy_open, create=True),
            self.assertRaises(FileBusy),
        ):
            self.wt.state({}, store_chunks=False)


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RestoreTestCase(WorktreeTestCase):
    # 版 A(a=a0, b=b0, d/c=c0)から版 B(a=a1, b=b1, e=e1、d/c は無し)への復元を試す。
    def setUp(self):
        super().setUp()
        (self.bvc / "txn").mkdir()
        self.write("a.bin", b"a1")
        self.write("b.bin", b"b1")
        self.write("e.bin", b"e1")
        self.tree_b = self.wt.state({}, store_chunks=True).tree
        for p in ("a.bin", "b.bin", "e.bin"):
            (self.tmp / p).unlink()
        self.write("a.bin", b"a0")
        self.write("b.bin", b"b0")
        self.write("d/c.bin", b"c0")
        self.heads = []

    def files(self):
        return {p: (self.tmp / p).read_bytes() for p in helpers.tree_hashes(self.tmp)}

    def on_committed(self, head):
        self.heads.append(head)

    def restore(self, **kw):
        current = self.wt.state({}, store_chunks=True)
        return self.wt.restore(
            self.tree_b, current, Head(1, 0), self.on_committed, **kw
        )

    def journal(self):
        return json.loads((self.bvc / "journal.json").read_text("utf-8"))

    def leave_swapping(self, stage="replace:swap:2"):
        # 置き換えの途中で失敗し、元に戻す処理もできなかった状態(強制終了の代わり)を作る
        before = self.files()
        with (
            mock.patch.object(fsutil, "_fault_hook", helpers.FaultAt(stage)),
            mock.patch.object(Worktree, "_rollback", side_effect=OSError("rollback")),
            self.assertRaises(FileBusy),
        ):
            self.restore()
        self.assertEqual(self.journal()["state"], "swapping")
        self.assertNotEqual(self.files(), before)
        return before


class TestRestore(RestoreTestCase):
    def test_restore_ops_and_index(self):
        r = self.restore()
        self.assertEqual(
            (r.written, r.deleted), (["a.bin", "b.bin", "e.bin"], ["d/c.bin"])
        )
        self.assertEqual(self.files(), {"a.bin": b"a1", "b.bin": b"b1", "e.bin": b"e1"})
        self.assertEqual(self.heads, [Head(1, 0)])
        self.assertFalse((self.bvc / "journal.json").exists())
        self.assertEqual(list((self.bvc / "txn").iterdir()), [])
        index = json.loads((self.bvc / "index.json").read_text("utf-8"))
        self.assertEqual(
            {p: e["manifest"] for p, e in index["entries"].items()}, self.tree_b
        )
        self.assertFalse(self.wt.state(self.tree_b, store_chunks=False).dirty)

    def test_keep_same_content(self):
        self.write("a.bin", b"a1")
        before = os.stat(self.tmp / "a.bin").st_mtime_ns
        r = self.restore()
        self.assertNotIn("a.bin", r.written)
        self.assertEqual(os.stat(self.tmp / "a.bin").st_mtime_ns, before)

    def test_no_ops(self):
        for p in ("a.bin", "b.bin", "d/c.bin"):
            (self.tmp / p).unlink()
        self.write("a.bin", b"a1")
        self.write("b.bin", b"b1")
        self.write("e.bin", b"e1")
        r = self.restore()
        self.assertEqual((r.written, r.deleted), ([], []))
        self.assertEqual(self.heads, [Head(1, 0)])

    def test_refuses_when_journal_exists(self):
        self.leave_swapping()
        with self.assertRaises(BvcError):
            self.restore()

    def test_staging_corruption_is_broken_version(self):
        sha = self.tree_b["b.bin"]
        m = self.store.get_manifest(sha)
        helpers.flip_byte(self.store.chunk_path(m.chunks[0].sha))
        before = self.files()
        with self.assertRaises(BrokenVersion):
            self.restore()
        self.assertEqual(self.files(), before)
        self.assertFalse((self.bvc / "journal.json").exists())
        self.assertEqual(self.heads, [])


class TestRecover(RestoreTestCase):
    def test_recover_swapping_rolls_back(self):
        before = self.leave_swapping()
        with self.assertLogs("bvc.worktree", "WARNING"):
            self.wt.recover(self.on_committed)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.heads, [])
        self.assertFalse((self.bvc / "journal.json").exists())
        self.assertEqual(list((self.bvc / "txn").iterdir()), [])

    def test_r3_recover_interrupted_is_idempotent(self):
        before = self.leave_swapping("replace:swap:3")
        for stage in (
            "replace:unswap:2",
            "replace:unstash:1",
            "txn_cleanup",
            "remove:journal.json",
        ):
            with self.subTest(stage=stage):
                with (
                    mock.patch.object(fsutil, "_fault_hook", helpers.FaultAt(stage)),
                    self.assertRaises(OSError),
                ):
                    self.wt.recover(self.on_committed)
                self.assertTrue((self.bvc / "journal.json").exists())
        with self.assertLogs("bvc.worktree", "WARNING"):
            self.wt.recover(self.on_committed)
        self.assertEqual(self.files(), before)
        self.wt.recover(self.on_committed)  # journal が無ければ何もしない
        self.assertEqual(self.files(), before)

    def test_recover_swapped_completes(self):
        def fail(head):
            raise OSError("HEAD を書けない")

        current = self.wt.state({}, store_chunks=True)
        with self.assertRaises(BvcError):
            self.wt.restore(self.tree_b, current, Head(1, 0), fail)
        self.assertEqual(self.journal()["state"], "swapped")
        wt = self.make(["**"])  # 開き直した状態(stat の記録なし)
        with self.assertLogs("bvc.worktree", "WARNING"):
            wt.recover(self.on_committed)
        self.assertEqual(self.heads, [Head(1, 0)])
        self.assertEqual(self.files(), {"a.bin": b"a1", "b.bin": b"b1", "e.bin": b"e1"})
        self.assertFalse((self.bvc / "journal.json").exists())
        self.assertFalse(wt.state(self.tree_b, store_chunks=False).dirty)

    def test_recover_staging_discards(self):
        before = self.files()
        with (
            mock.patch.object(fsutil, "_fault_hook", helpers.FaultAt("stage:2")),
            mock.patch.object(Worktree, "_discard_txn"),
            self.assertRaises(FileBusy),
        ):
            self.restore()
        self.assertEqual(self.journal()["state"], "staging")
        with self.assertLogs("bvc.worktree", "WARNING"):
            self.wt.recover(self.on_committed)
        self.assertEqual(self.files(), before)
        self.assertEqual(list((self.bvc / "txn").iterdir()), [])

    def test_p1_tampered_journal_changes_nothing(self):
        self.leave_swapping()
        good = self.journal()
        bad_paths = [
            "../x.bin",
            "/abs.bin",
            "C:/x.bin",
            "//server/x.bin",
            ".bvc/config.json",
            "CON",
            "",
            "a\x01.bin",
            "a\\b.bin",
        ]
        cases = [("ops.path", p) for p in bad_paths] + [
            ("ops.src", p) for p in bad_paths
        ]
        cases += [("target", p) for p in bad_paths]
        cases += [
            ("ops.n", -1),
            ("ops.n", "0"),
            ("head.at", 1.5),
            ("ops.sha", "../" + "a" * 61),
            ("state", "x"),
        ]
        work = helpers.tree_hashes(self.tmp)
        txn = helpers.tree_hashes(self.bvc / "txn", exclude=())
        config = helpers.tree_hashes(self.bvc, exclude=("txn", "tmp"))
        for where, value in cases:
            with self.subTest(where=where, value=value):
                j = json.loads(json.dumps(good))
                if where == "ops.path":
                    j["ops"][0]["path"] = value
                elif where == "ops.src":
                    j["ops"][0]["src"] = value
                elif where == "target":
                    j["target"][value] = "a" * 64
                elif where == "ops.n":
                    j["ops"][0]["n"] = value
                elif where == "head.at":
                    j["head"]["at"] = value
                elif where == "ops.sha":
                    j["ops"][1]["sha"] = value
                else:
                    j[where] = value
                fsutil.atomic_write_json(self.bvc / "journal.json", j, self.bvc / "tmp")
                config["journal.json"] = helpers.sha256_file(self.bvc / "journal.json")
                with self.assertRaises(CorruptData):
                    self.wt.recover(self.on_committed)
                self.assertEqual(helpers.tree_hashes(self.tmp), work)
                self.assertEqual(helpers.tree_hashes(self.bvc / "txn", exclude=()), txn)
                self.assertEqual(
                    helpers.tree_hashes(self.bvc, exclude=("txn", "tmp")), config
                )
        self.assertEqual(self.heads, [])
        fsutil.atomic_write_json(self.bvc / "journal.json", good, self.bvc / "tmp")
        with self.assertLogs("bvc.worktree", "WARNING"):
            self.wt.recover(self.on_committed)  # 正しい記録に戻せば元に戻せる

    def test_leftovers_without_journal(self):
        (self.bvc / "txn" / "new").mkdir()
        (self.bvc / "txn" / "new" / "0").write_bytes(b"garbage")
        self.wt.recover(self.on_committed)
        self.assertFalse((self.bvc / "txn" / "new").exists())
        (self.bvc / "txn" / "old").mkdir()
        (self.bvc / "txn" / "old" / "0").write_bytes(b"maybe precious")
        with self.assertLogs("bvc.worktree", "WARNING"):
            self.wt.recover(self.on_committed)
        self.assertTrue((self.bvc / "txn" / "old" / "0").exists())
        with self.assertRaises(BvcError):
            self.restore()
        self.assertTrue((self.bvc / "txn" / "old" / "0").exists())


class TestIndexCheck(WorktreeTestCase):
    # M4-10: index.json の検査と作り直し(C-7)。
    def test_check_and_reset(self):
        self.write("a", b"1")
        self.assertEqual(self.wt.check_index(), "missing")
        st = self.wt.state({}, store_chunks=True)
        self.wt.update_index(st.tree, st.fs_time_ns)
        self.assertIsNone(self.wt.check_index())
        index = self.bvc / "index.json"
        for content in (
            b"",
            b"{",
            b'{"format":1,"fs_time_ns":0,"entries":{"../x":{}}}',
        ):
            with self.subTest(content=content):
                index.write_bytes(content)
                self.assertIsNotNone(self.wt.check_index())
                self.wt.reset_index()
                self.assertIsNone(self.wt.check_index())
                self.assertEqual(self.wt._load_index(), ({}, 0))
        index.write_bytes(b'{"format":7}')
        with self.assertRaises(UnsupportedFormat):
            self.wt.check_index()


class TestRepairCandidates(WorktreeTestCase):
    # M4-9: verify --repair の材料(仕様書 3.10節)。
    def test_tracked_and_untracked_by_name_or_size(self):
        wt = self.make(["*.bin"])
        self.write("a.bin", b"1")
        self.write("sub/a.bin", b"22")  # パターン外・名前が一致
        self.write("sub/A.BIN.x", b"22")  # 名前が違う
        self.write("x.dat", b"333")  # サイズが一致
        self.write("y.dat", b"4444")
        (self.bvc / "a.bin").write_bytes(b"1")  # .bvc の中は見ない
        self.assertEqual(
            wt.repair_candidates({"A.bin"}, {3}), ["a.bin", "sub/a.bin", "x.dat"]
        )
        if helpers.try_symlink(self.tmp / "y.dat", self.tmp / "link.dat"):
            self.assertNotIn("link.dat", wt.repair_candidates(set(), {4}))


if __name__ == "__main__":
    unittest.main()
