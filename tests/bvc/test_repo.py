# repo の単体テスト(M2-2, M2-10)。観点: F-1, F-7, F-12, P-5, R-1, R-7, C-3。

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from bvc import fsutil, worktree
from bvc.errors import BvcError, Locked, MissingFiles, UsageError
from bvc.model import Head
from bvc.repo import Repo, parse_config
from tests import helpers


def commit_files(bvc: Path) -> dict[int, dict]:
    out = {}
    for p in (bvc / "commits").glob("*.json"):
        out[int(p.stem)] = json.loads(p.read_text("utf-8"))
    return out


class RepoTestCase(helpers.TempDirTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(worktree, "RETRY_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def init(self, track=("**",), **kw):
        repo = Repo.init(self.tmp, track=list(track), **kw)
        self.addCleanup(repo.close)
        return repo

    def reopen(self, repo=None, start=None):
        if repo is not None:
            repo.close()
        repo = Repo.open(start or self.tmp)
        self.addCleanup(repo.close)
        return repo

    def write(self, rel, data=b"x"):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class TestInit(RepoTestCase):
    def test_f1_existing_files_recorded_in_version_0(self):
        self.write("a.bin", b"a")
        self.write("sub/b.bin", b"b")
        self.write("note.txt", b"n")
        repo = self.init(["**/*.bin"])
        c0 = repo.log()[0].commit
        self.assertEqual((c0.id, c0.kind), (0, "init"))
        self.assertEqual(sorted(c0.tree), ["a.bin", "sub/b.bin"])
        for sha in c0.tree.values():
            self.assertTrue(repo._store.manifest_ok(sha, "full"))
        # 保存データは .bvc の中だけに書く
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".bvc", "a.bin", "note.txt", "sub"])
        self.assertEqual(repo._history.head(), Head(0, 0))

    def test_empty_warns(self):
        with self.assertLogs("bvc.repo", "WARNING"):
            self.init(["*.bin"])

    def test_already_exists(self):
        self.init().close()
        (self.tmp / "sub").mkdir()
        for target in (self.tmp, self.tmp / "sub"):
            with self.subTest(target=target), self.assertRaises(BvcError):
                Repo.init(target, track=["*"])

    def test_invalid_config_creates_nothing(self):
        for kw in ({"track": []}, {"track": ["*"], "compression": "lzma"},
                   {"track": ["*"], "chunker": {"name": "fixed", "size": 0}}):
            with self.subTest(kw=kw), self.assertRaises(UsageError):
                Repo.init(self.tmp, **kw)
            self.assertFalse((self.tmp / ".bvc").exists())

    def test_failure_removes_new_bvc_only(self):
        self.write("a.bin", b"a")
        hook = helpers.FaultAt("atomic_write:HEAD.json")
        with mock.patch.object(fsutil, "_fault_hook", hook), self.assertRaises(OSError):
            Repo.init(self.tmp, track=["*"])
        self.assertFalse((self.tmp / ".bvc").exists())
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"a")
        self.init().close()  # やり直せる


class TestConfig(unittest.TestCase):
    def test_i17_invalid_values(self):
        base = {"format": 1, "track": ["*"]}
        for patch in ({"track": "x"}, {"ignore": [1]}, {"chunker": {"name": "nope"}},
                      {"compression": "x"}, {"threads": -1}, {"verify_chunks": "x"},
                      {"rules": [{"chunker": {"name": "whole"}}]},
                      {"rules": [{"pattern": "*", "compression": "x"}]}):
            with self.subTest(patch=patch), self.assertRaises(UsageError):
                parse_config({**base, **patch})
        self.assertEqual(parse_config(base).chunker["name"], "fixed")


class TestCommit(RepoTestCase):
    def test_f12_no_change(self):
        self.write("a", b"1")
        repo = self.init()
        r = repo.commit("m")
        self.assertFalse(r.changed)
        self.assertEqual(len(commit_files(repo.bvc_dir)), 1)

    def test_commit_and_oplog(self):
        repo = self.init()
        self.write("a", b"1")
        r = repo.commit("add a")
        self.assertTrue(r.changed)
        self.assertEqual((r.commit.id, r.commit.parent, r.state.added), (1, 0, ["a"]))
        self.assertFalse(r.new_branch)
        self.assertEqual(r.commit.stats["total_bytes"], 1)
        self.assertEqual(repo._history.head(), Head(1, 0))
        ops, _ = fsutil.read_jsonl(repo.bvc_dir / "oplog.jsonl", "oplog")
        self.assertEqual([o["op"] for o in ops], ["init", "commit"])
        self.assertEqual(ops[-1]["after"], {"at": 1, "branch": 0})

    def test_f2_new_branch_from_middle(self):
        repo = self.init()
        self.write("a", b"1")
        repo.commit()
        self.write("a", b"2")
        repo.commit()
        repo._history.set_head(Head(1, 0))  # undo の代わり(M3)
        self.write("a", b"3")
        r = repo.commit()
        self.assertTrue(r.new_branch)
        self.assertEqual(repo._history.head(), Head(3, 1))

    def test_f7_missing_aborts_and_allow_missing_records_deletion(self):
        self.write("a", b"1")
        self.write("b", b"2")
        repo = self.init()
        (self.tmp / "a").unlink()
        with self.assertRaises(MissingFiles) as cm:
            repo.commit()
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertEqual(cm.exception.details["missing"], ["a"])
        self.assertEqual(len(commit_files(repo.bvc_dir)), 1)
        r = repo.commit(allow_missing=True)
        self.assertEqual(list(r.commit.tree), ["b"])

    def test_exact_rename_is_recorded(self):
        self.write("a", b"1")
        repo = self.init()
        (self.tmp / "a").rename(self.tmp / "b")
        r = repo.commit()
        self.assertEqual(r.commit.renames, (("a", "b", 1.0),))

    def test_manual_rename_not_yet_supported(self):
        repo = self.init()
        with self.assertRaises(UsageError):
            repo.commit(renames=[("a", "b")])


class TestOpenAndLock(RepoTestCase):
    def test_p5_open_from_subdir(self):
        self.write("a", b"1")
        self.write("sub/b", b"2")
        self.init().close()
        repo = self.reopen(start=self.tmp / "sub")
        self.assertEqual(repo.workdir, self.tmp)
        self.write("sub/b", b"3")
        r = repo.commit()
        self.assertEqual(r.state.modified, ["sub/b"])
        self.assertEqual(r.state.missing, [])

    def test_not_found(self):
        with self.assertRaises(BvcError):
            Repo.open(self.tmp)

    def test_r7_second_open_is_locked(self):
        repo = self.init()
        with self.assertRaises(Locked):
            Repo.open(self.tmp)
        repo.close()
        self.reopen()

    def test_lock_released_after_failed_open(self):
        self.init().close()
        (self.tmp / ".bvc" / "config.json").write_bytes(b"{")
        with self.assertRaises(BvcError):
            Repo.open(self.tmp)
        self.assertFalse((self.tmp / ".bvc" / "lock").exists())

    def test_lock_released_by_with_on_error(self):
        self.write("a", b"1")
        self.init().close()
        (self.tmp / "a").unlink()
        with self.assertRaises(MissingFiles):
            with Repo.open(self.tmp) as repo:
                repo.commit()
        self.assertFalse((self.tmp / ".bvc" / "lock").exists())


class _PrefixFault:
    # 段階名が正規表現に一致したら、count 回目に OSError を送出する。
    def __init__(self, pattern, count=1):
        self.pattern = re.compile(pattern)
        self.count = count
        self.seen = 0

    def __call__(self, stage):
        if self.pattern.fullmatch(stage):
            self.seen += 1
            if self.seen == self.count:
                raise OSError(f"注入した障害: {stage}")


class TestCommitFaults(RepoTestCase):
    # R-1: commit の各書き込み段階で例外を起こしても、その後の commit・log が正常に動く
    STAGES = [
        "chunk_write",
        r"atomic_write:[0-9a-f]{64}\.json",
        "atomic_write:counters.json",
        r"atomic_write:\d+\.json",
        "atomic_write:HEAD.json",
        "atomic_write:index.json",
        "append_jsonl:oplog.jsonl",
    ]

    def test_r1_each_stage(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                helpers.remove_tree(self.tmp)
                self.tmp.mkdir()
                self.write("a", b"1")
                repo = Repo.init(self.tmp, track=["**"])
                self.write("a", helpers.random_bytes(1000, seed=len(stage)))
                hook = _PrefixFault(stage)
                with mock.patch.object(fsutil, "_fault_hook", hook), self.assertRaises(Exception):
                    repo.commit("first")
                self.assertEqual(hook.seen, 1, "段階が呼ばれていない")
                repo.close()

                with Repo.open(self.tmp) as repo:
                    repo.commit("retry")
                    self.write("b", b"2")
                    r = repo.commit("next")
                    self.assertTrue(r.changed)
                    head = repo._history.head()
                    tree = repo._history.get(head.at).tree
                    self.assertEqual(sorted(tree), ["a", "b"])
                    self.assertEqual(repo.work_state().dirty, False)
                    entries = repo.log()
                files = commit_files(self.tmp / ".bvc")
                self.assertEqual([e.id for e in entries], sorted(files, reverse=True))
                for cid, data in files.items():
                    self.assertEqual(data["id"], cid)


class TestLog(RepoTestCase):
    def test_c3_log_with_unreadable_commit(self):
        repo = self.init()
        for i in range(4):
            self.write("a", str(i).encode())
            repo.commit()
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "2.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        entries = {e.id: e for e in repo.log()}
        self.assertTrue(entries[2].broken)
        self.assertIsNone(entries[2].commit)
        self.assertEqual(entries[3].effective_parent, 2)
        self.assertEqual(entries[2].effective_parent, 1)
        self.assertTrue(entries[4].is_current)

    def test_limit(self):
        repo = self.init()
        for i in range(3):
            self.write("a", str(i).encode())
            repo.commit()
        self.assertEqual([e.id for e in repo.log(limit=2)], [3, 2])


if __name__ == "__main__":
    unittest.main()
