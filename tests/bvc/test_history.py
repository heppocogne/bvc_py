# history の単体テスト(M2-3〜M2-6)。観点: F-2, F-5, C-3, C-8, R-1(版の書き込み部分), V-2。

import json
import unittest
from pathlib import Path
from unittest import mock

from bvc import fsutil
from bvc.errors import CorruptData, IntegrityError, RevisionError, UnsupportedFormat
from bvc.history import History, check_branch_name
from bvc.model import Head
from tests import helpers

SHA = "a" * 64


def make_bvc(root: Path) -> Path:
    bvc = root / ".bvc"
    for d in ("commits", "tmp"):
        (bvc / d).mkdir(parents=True)
    fsutil.atomic_write_json(bvc / "counters.json", {"format": 1, "next_commit": 0, "next_branch": 0}, bvc / "tmp")
    return bvc


def load(bvc: Path) -> History:
    h = History(bvc)
    h.load()
    return h


class HistoryTestCase(helpers.TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.bvc = make_bvc(self.tmp)
        self.h = load(self.bvc)

    def commit(self, parent, tree=None, h=None):
        return (h or self.h).new_commit(parent, tree or {}, "commit", "m")

    def linear(self, n):
        # 0 ← 1 ← … ← n-1 の一本道を作る
        self.h.new_commit(None, {}, "init", "")
        for i in range(1, n):
            self.commit(i - 1)

    def write_branches(self, names):
        fsutil.atomic_write_json(self.bvc / "branches.json", {"format": 1, "names": names}, self.bvc / "tmp")

    def discard(self, cid):
        fsutil.append_jsonl(self.bvc / "discarded.jsonl", {"format": 1, "time": "t", "id": cid})


class TestBranchRule(HistoryTestCase):
    def test_f2_extend_at_tip_and_branch_from_middle(self):
        c0 = self.h.new_commit(None, {}, "init", "")
        c1 = self.commit(0)
        c2 = self.commit(1)
        self.assertEqual({c0.branch, c1.branch, c2.branch}, {0})
        c3 = self.commit(1)  # 1 には子 2 があるので新しいブランチ
        self.assertEqual(c3.branch, 1)
        c4 = self.commit(3)
        self.assertEqual(c4.branch, 1)
        self.assertEqual(self.h.children(1), [2, 3])
        self.assertEqual(self.h.branch_tip(0), 2)
        self.assertEqual(self.h.path_to_tip(1), [4, 3, 1, 0])

    def test_f2_discarded_child_does_not_count(self):
        self.linear(3)
        self.discard(2)
        h = load(self.bvc)
        self.assertEqual(h.children(1), [])
        self.assertEqual(self.commit(1, h=h).branch, 0)

    def test_ancestors_recorded(self):
        self.linear(4)
        data = json.loads((self.bvc / "commits" / "3.json").read_text("utf-8"))
        self.assertEqual(data["parent"], 2)
        self.assertEqual(data["ancestors"], [2, 1, 0])

    def test_persisted_and_reloaded(self):
        self.linear(3)
        self.commit(1)
        h = load(self.bvc)
        self.assertEqual(h.ids(), [3, 2, 1, 0])
        self.assertEqual(h.get(3).branch, 1)


class TestRevision(HistoryTestCase):
    def setUp(self):
        super().setUp()
        # 0 ← 1 ← 2 ← 3(ブランチ 0)、1 ← 4 ← 5(ブランチ 1、名前 "exp")
        self.linear(4)
        self.commit(1)
        self.commit(4)
        self.write_branches({"1": "exp"})
        self.h = load(self.bvc)

    def r(self, expr, at=2, branch=0):
        return self.h.resolve(expr, Head(at, branch))

    def test_f5_valid(self):
        self.assertEqual(self.r("0"), 0)
        self.assertEqual(self.r("@"), 2)
        self.assertEqual(self.r("@-"), 1)
        self.assertEqual(self.r("@--"), 0)
        self.assertEqual(self.r("@+"), 3)
        self.assertEqual(self.r("5-"), 4)
        self.assertEqual(self.r("exp"), 5)
        self.assertEqual(self.r("exp--"), 1)
        self.assertEqual(self.r("@-+"), 2)

    def test_f5_plus_follows_head_branch(self):
        # 1 には子が2つあるが、HEAD.branch の経路上ならその方向へ進む
        self.assertEqual(self.r("1+", at=1, branch=0), 2)
        self.assertEqual(self.r("1+", at=1, branch=1), 4)

    def test_f5_errors(self):
        for expr in ("99", "0-", "3+", "5+", "", "@x", "-", "01", "nobranch", "@-3", "a b"):
            with self.subTest(expr=expr), self.assertRaises(RevisionError):
                self.r(expr)

    def test_f5_plus_ambiguous_off_branch(self):
        # HEAD.branch の経路に無い版で子が複数なら決められない
        self.commit(4)  # 4 の子が 5 と 6 になる
        h = load(self.bvc)
        with self.assertRaises(RevisionError):
            h.resolve("4+", Head(4, 0))

    def test_f5_discarded_number(self):
        self.discard(3)
        h = load(self.bvc)
        with self.assertRaises(RevisionError):
            h.resolve("3", Head(2, 0))
        with self.assertRaises(RevisionError):
            h.resolve("@+", Head(2, 0))  # 先端の 3 が消えたので 2 が先端

    def test_branch_name_rules(self):
        for bad in ("123", "@x", "a-b", "a+b", "a b", ""):
            with self.subTest(name=bad), self.assertRaises(RevisionError):
                check_branch_name(bad)
        self.assertEqual(check_branch_name("細分化"), "細分化")


class TestBrokenCommit(HistoryTestCase):
    def test_c3_unreadable_commit_is_bridged_by_ancestors(self):
        self.linear(5)
        helpers.break_json(self.bvc / "commits" / "2.json")
        with self.assertLogs("bvc.history", "WARNING"):
            h = load(self.bvc)
        self.assertFalse(h.is_readable(2))
        self.assertTrue(h.is_broken(2))
        self.assertEqual(h.ids(), [4, 3, 2, 1, 0])
        self.assertEqual(h.effective_parent(3), 2)
        self.assertEqual(h.effective_parent(2), 1)  # 子(3, 4)の ancestors から補う
        self.assertEqual(h.path_to_tip(0), [4, 3, 2, 1, 0])
        with self.assertRaises(RevisionError):
            h.get(2)
        self.assertEqual(h.resolve("@--", Head(3, 0)), 1)

    def test_c3_discarded_unreadable_is_skipped(self):
        self.linear(4)
        helpers.break_json(self.bvc / "commits" / "2.json")
        self.discard(2)
        with self.assertLogs("bvc.history", "WARNING"):
            h = load(self.bvc)
        self.assertEqual(h.effective_parent(3), 1)

    def test_invalid_contents_are_unreadable(self):
        self.linear(3)
        path = self.bvc / "commits" / "2.json"
        original = json.loads(path.read_text("utf-8"))
        for patch in ({"id": 5}, {"parent": 2}, {"ancestors": [0, 1]}, {"kind": "x"}, {"branch": -1}):
            with self.subTest(patch=patch):
                fsutil.atomic_write_json(path, {**original, **patch}, self.bvc / "tmp")
                with self.assertLogs("bvc.history", "WARNING"):
                    h = load(self.bvc)
                self.assertFalse(h.is_readable(2))

    def test_unsafe_tree_path_marks_broken(self):
        self.h.new_commit(None, {"../x": SHA, "ok.bin": SHA}, "init", "")
        with self.assertLogs("bvc.history", "WARNING"):
            h = load(self.bvc)
        self.assertTrue(h.is_readable(0))
        self.assertTrue(h.is_broken(0))
        self.assertEqual(h.get(0).tree, {"ok.bin": SHA})

    def test_v2_unknown_format_aborts(self):
        self.linear(2)
        path = self.bvc / "commits" / "1.json"
        data = json.loads(path.read_text("utf-8"))
        data["format"] = 99
        fsutil.atomic_write_json(path, data, self.bvc / "tmp")
        with self.assertRaises(UnsupportedFormat):
            load(self.bvc)

    def test_c8_broken_jsonl_line_warns(self):
        self.linear(3)
        (self.bvc / "discarded.jsonl").write_bytes(b"{broken\n" + fsutil.canonical_json({"format": 1, "id": 2}) + b"\n")
        with self.assertLogs("bvc.history", "WARNING"):
            h = load(self.bvc)
        self.assertTrue(h.is_discarded(2))

    def test_broken_branches_json_is_ignored(self):
        self.linear(2)
        (self.bvc / "branches.json").write_bytes(b"{")
        with self.assertLogs("bvc.history", "WARNING"):
            h = load(self.bvc)
        self.assertIsNone(h.branch_name(0))


class TestNewCommitWriteOrder(HistoryTestCase):
    def test_r1_counters_before_commit(self):
        # 版の書き込み直前で失敗しても、counters は進んでいて、次の版は別の番号になる
        self.linear(2)
        hook = helpers.FaultAt("atomic_write:2.json")
        with mock.patch.object(fsutil, "_fault_hook", hook), self.assertRaises(OSError):
            self.commit(1)
        self.assertEqual(hook.calls[-2:], ["atomic_write:counters.json", "atomic_write:2.json"])
        self.assertFalse((self.bvc / "commits" / "2.json").exists())
        h = load(self.bvc)
        self.assertEqual(self.commit(1, h=h).id, 3)

    def test_stale_counters_never_overwrite(self):
        self.linear(3)
        before = (self.bvc / "commits" / "2.json").read_bytes()
        fsutil.atomic_write_json(self.bvc / "counters.json", {"format": 1, "next_commit": 1, "next_branch": 0}, self.bvc / "tmp")
        h = load(self.bvc)
        c = self.commit(2, h=h)
        self.assertEqual(c.id, 3)
        self.assertEqual((self.bvc / "commits" / "2.json").read_bytes(), before)

    def test_existing_unregistered_file_aborts(self):
        self.linear(2)
        (self.bvc / "commits" / "2.json").write_text("x", encoding="utf-8")
        # 読み込み後に現れた版ファイルは上書きしない
        with self.assertRaises(IntegrityError):
            self.commit(1)
        self.assertEqual((self.bvc / "commits" / "2.json").read_text("utf-8"), "x")

    def test_corrupt_counters_aborts(self):
        self.linear(1)
        (self.bvc / "counters.json").write_bytes(b"{")
        with self.assertRaises(CorruptData):
            self.commit(0)

    def test_log_op_appends(self):
        self.h.log_op({"op": "commit", "result": "ok"})
        self.h.log_op({"op": "commit", "result": "ok"})
        records, warns = fsutil.read_jsonl(self.bvc / "oplog.jsonl", "oplog")
        self.assertEqual([r["op"] for r in records], ["commit", "commit"])
        self.assertTrue(all("time" in r for r in records))


if __name__ == "__main__":
    unittest.main()
