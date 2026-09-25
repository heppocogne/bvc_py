# repo の単体テスト(M2-2, M2-10, M3-6, M3-7)。
# 観点: F-1, F-3, F-4, F-7, F-12, P-5, P-7, P-8, P-9, R-1, R-2, R-4, R-6, R-7, R-9, R-10, R-11, C-3。

import json
import os
import re
import shutil
import stat
import time
import unittest
from pathlib import Path
from unittest import mock

from bvc import fsutil, worktree
from bvc.errors import (
    BrokenVersion,
    BvcError,
    CannotMove,
    DiskFull,
    FileBusy,
    FileChanging,
    Locked,
    MissingFiles,
    RevisionError,
    SafetyAbort,
    UsageError,
)
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
        repo.undo()
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


class MoveTestCase(RepoTestCase):
    # 移動系(M3)のテストの共通処理。
    def head(self, repo):
        return repo._history.head()

    def files(self):
        return {p: (self.tmp / p).read_bytes() for p in helpers.tree_hashes(self.tmp)}

    def assert_clean_at(self, repo, at):
        # HEAD が at で、作業フォルダが版 at と一致し、作業域・journal が残っていない
        self.assertEqual(self.head(repo).at, at)
        self.assertFalse(repo.work_state().dirty)
        self.assertFalse((repo.bvc_dir / "journal.json").exists())
        self.assertEqual(list((repo.bvc_dir / "txn").iterdir()), [])

    def build_linear(self, n=2):
        # 版 0..n。a.bin の内容は版番号
        self.write("a.bin", b"v0")
        repo = self.init(["**/*.bin"])
        for i in range(1, n + 1):
            self.write("a.bin", f"v{i}".encode())
            repo.commit(f"c{i}")
        return repo

    def oplog(self, repo):
        return fsutil.read_jsonl(repo.bvc_dir / "oplog.jsonl", "oplog")[0]


class TestUndoRedoGoto(MoveTestCase):
    def test_f1_undo_redo_restore_files(self):
        self.write("a.bin", b"0")
        repo = self.init(["**/*.bin"])
        self.write("a.bin", b"1")
        self.write("sub/b.bin", b"b")
        repo.commit("one")
        r = repo.undo(reason="確認")
        self.assertTrue(r.changed)
        self.assertEqual((r.before, r.after), (Head(1, 0), Head(0, 0)))
        self.assertIsNone(r.auto_commit)
        self.assertEqual((r.restored, r.deleted), (["a.bin"], ["sub/b.bin"]))
        self.assertEqual(self.files(), {"a.bin": b"0"})
        self.assertTrue((self.tmp / "sub").is_dir())  # P-9: 空になったフォルダは残す
        self.assert_clean_at(repo, 0)
        r = repo.redo()
        self.assertEqual(self.files(), {"a.bin": b"1", "sub/b.bin": b"b"})
        self.assert_clean_at(repo, 1)
        ops = self.oplog(repo)
        self.assertEqual([o["op"] for o in ops], ["init", "commit", "undo", "redo"])
        self.assertEqual((ops[2]["reason"], ops[2]["after"], ops[2]["created"]), ("確認", {"at": 0, "branch": 0}, []))

    def test_f12_undo_at_root_and_redo_at_tip(self):
        repo = self.build_linear(1)
        before = self.files()
        with self.assertRaises(CannotMove) as cm:
            repo.redo()
        self.assertEqual(cm.exception.exit_code, 4)
        repo.undo()
        self.write("a.bin", b"edit")  # 移動できない場合は、未コミットの変更があっても何も作らない
        with self.assertRaises(CannotMove):
            repo.undo()
        self.assertEqual(len(commit_files(repo.bvc_dir)), 2)
        self.assertEqual(self.head(repo), Head(0, 0))
        self.assertNotEqual(before, self.files())
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")

    def test_f3_roundtrip_with_branch(self):
        repo = self.build_linear(2)          # 0 - 1 - 2(ブランチ 0)
        repo.undo()                          # @ = 1
        self.write("a.bin", b"v3")
        r = repo.commit("c3")                # 1 - 3(ブランチ 1)
        self.assertTrue(r.new_branch)
        self.assertEqual(self.head(repo), Head(3, 1))
        # undo → redo は元のブランチ(1)の方向へ戻る
        repo.undo()
        repo.undo()
        self.assertEqual(self.head(repo), Head(0, 1))
        repo.redo()
        repo.redo()
        self.assertEqual(self.head(repo), Head(3, 1))
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v3")
        # goto の後は、移動先のブランチの方向へ redo する
        r = repo.goto("2")
        self.assertEqual(r.after, Head(2, 0))
        repo.undo()
        self.assertEqual(self.head(repo), Head(1, 0))
        repo.redo()
        self.assertEqual(self.head(repo), Head(2, 0))
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v2")
        self.assert_clean_at(repo, 2)

    def test_f4_auto_commit_and_redo_back(self):
        repo = self.build_linear(1)
        self.write("a.bin", b"edit")
        self.write("new.bin", b"n")
        r = repo.undo()
        auto = r.auto_commit
        self.assertEqual((auto.kind, auto.message, auto.parent), ("auto", "auto: before undo", 1))
        self.assertEqual(r.after, Head(0, auto.branch))
        self.assertEqual(self.files(), {"a.bin": b"v0"})
        self.assertEqual(self.oplog(repo)[-1]["created"], [auto.id])
        repo.redo()
        repo.redo()
        self.assertEqual(self.head(repo).at, auto.id)
        self.assertEqual(self.files(), {"a.bin": b"edit", "new.bin": b"n"})
        # goto の自動コミットのメッセージには引数が入る
        self.write("a.bin", b"edit2")
        r = repo.goto("0")
        self.assertEqual(r.auto_commit.message, "auto: before goto 0")

    def test_f4_auto_commit_with_children_makes_new_branch(self):
        repo = self.build_linear(2)
        repo.undo()                          # @ = 1(子 2 がある)
        self.write("a.bin", b"edit")
        r = repo.redo()
        self.assertTrue(r.auto_commit.branch != 0)
        self.assertEqual(r.after, Head(2, 0))  # redo は元のブランチのまま進む
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v2")
        repo.goto(str(r.auto_commit.id))
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")

    def test_f7_missing_on_move(self):
        self.write("b.bin", b"b")
        repo = self.build_linear(1)
        (self.tmp / "b.bin").unlink()
        with self.assertRaises(MissingFiles) as cm:
            repo.undo()
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertEqual(self.head(repo), Head(1, 0))
        self.assertEqual(len(commit_files(repo.bvc_dir)), 2)
        r = repo.undo(allow_missing=True)
        self.assertNotIn("b.bin", r.auto_commit.tree)
        self.assertEqual(self.files(), {"a.bin": b"v0", "b.bin": b"b"})

    def test_f12_goto_current_is_no_change(self):
        repo = self.build_linear(1)
        n_ops = len(self.oplog(repo))
        for rev in ("@", "1"):
            with self.subTest(rev=rev):
                r = repo.goto(rev)
                self.assertFalse(r.changed)
                self.assertEqual(r.after, Head(1, 0))
        self.assertEqual(len(self.oplog(repo)), n_ops)

    def test_goto_errors(self):
        repo = self.build_linear(1)
        for rev in ("9", "@+", "0-", "nobranch", "@@"):
            with self.subTest(rev=rev), self.assertRaises(RevisionError):
                repo.goto(rev)
        self.assert_clean_at(repo, 1)

    def test_redo_with_multiple_children(self):
        repo = self.build_linear(1)
        repo.goto("0")
        self.write("a.bin", b"x")
        repo.commit("x")                     # 0 の子は 1 と 2
        repo._history.set_head(Head(0, 99))  # 経路上に無い状態を作る
        self.write("a.bin", b"v0")
        with self.assertRaises(CannotMove) as cm:
            repo.redo()
        self.assertEqual(cm.exception.details["candidates"], [1, 2])

    def test_skip_broken_not_yet_supported(self):
        repo = self.build_linear(1)
        with self.assertRaises(UsageError):
            repo.undo(skip_broken=True)

    def test_p7_case_only_rename_roundtrip(self):
        self.write("a.bin", b"1")
        repo = self.init(["**/*.bin"])
        (self.tmp / "a.bin").rename(self.tmp / "A.bin")
        r = repo.commit()
        self.assertEqual(r.commit.renames, (("a.bin", "A.bin", 1.0),))
        repo.undo()
        self.assertEqual(os.listdir(self.tmp), [".bvc", "a.bin"])
        repo.redo()
        self.assertEqual(os.listdir(self.tmp), [".bvc", "A.bin"])
        self.assert_clean_at(repo, 1)

    def test_p9_folders_increase_and_decrease(self):
        self.write("a.bin", b"1")
        repo = self.init(["**/*.bin"])
        self.write("x/y/z.bin", b"z")
        repo.commit()
        (self.tmp / "a.bin").unlink()
        repo.commit(allow_missing=True)
        repo.goto("0")
        self.assertEqual(self.files(), {"a.bin": b"1"})
        self.assertTrue((self.tmp / "x" / "y").is_dir())
        (self.tmp / "x" / "y").rmdir()
        (self.tmp / "x").rmdir()
        repo.goto("2")
        self.assertEqual(self.files(), {"x/y/z.bin": b"z"})

    def test_p8_readonly_files(self):
        self.write("a.bin", b"1")
        self.write("b.bin", b"b")
        repo = self.init(["**/*.bin"])
        self.write("a.bin", b"2")
        (self.tmp / "b.bin").unlink()
        self.write("c.bin", b"c")
        repo.commit(allow_missing=True)
        for name in ("a.bin", "c.bin"):
            os.chmod(self.tmp / name, stat.S_IREAD)
        repo.undo()  # 読み取り専用のファイルを置き換え・削除できる
        self.assertEqual(self.files(), {"a.bin": b"1", "b.bin": b"b"})
        self.assert_clean_at(repo, 0)

    def test_broken_target_changes_nothing(self):
        repo = self.build_linear(2)
        sha = repo._history.get(0).tree["a.bin"]
        helpers.flip_byte(repo._store.manifest_path(sha))
        self.write("a.bin", b"edit")
        with self.assertRaises(BrokenVersion):
            repo.goto("0")
        self.assertEqual(self.head(repo), Head(2, 0))
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")
        self.assertEqual(len(commit_files(repo.bvc_dir)), 3)
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "1.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        with self.assertRaises(BrokenVersion):
            repo.goto("1")
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")


class TestMoveSafety(MoveTestCase):
    def test_i18_rollback_leaves_head_at_auto(self):
        repo = self.build_linear(1)
        self.write("a.bin", b"edit")
        hook = helpers.FaultAt("replace:swap:0")
        with mock.patch.object(fsutil, "_fault_hook", hook), self.assertRaises(FileBusy) as cm:
            repo.undo()
        self.assertEqual(cm.exception.exit_code, 3)
        auto_id = 2
        self.assertEqual(repo._history.get(auto_id).kind, "auto")
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")
        self.assert_clean_at(repo, auto_id)
        self.assertEqual(self.oplog(repo)[-1]["result"], "error")
        # 次の commit は auto のブランチを延長する(新しいブランチを作らない)
        self.write("a.bin", b"edit2")
        r = repo.commit()
        self.assertFalse(r.new_branch)
        self.assertEqual(r.commit.parent, auto_id)

    def test_r2_fault_at_each_step_rolls_back_everything(self):
        # 削除 1 件 + 書き出し 3 件(置き換え 2 件、新規 1 件)の各段階で例外を起こす
        def build():
            helpers.remove_tree(self.tmp)
            self.tmp.mkdir()
            self.write("a.bin", b"a0")
            self.write("b.bin", b"b0")
            self.write("d/c.bin", b"c0")
            repo = Repo.init(self.tmp, track=["**/*.bin"])
            self.write("a.bin", b"a1")
            self.write("b.bin", b"b1")
            (self.tmp / "d" / "c.bin").unlink()
            self.write("e.bin", b"e1")
            repo.commit(allow_missing=True)
            self.write("a.bin", b"a-edit")  # 自動コミットを伴う
            return repo

        stages = (
            [("atomic_write:journal.json", k) for k in range(1, 8)]
            + [(f"stage:{n}", 1) for n in (1, 2, 3)]
            + [(f"replace:stash:{n}", 1) for n in (0, 1, 2)]
            + [(f"replace:swap:{n}", 1) for n in (1, 2, 3)]
        )
        for stage, count in stages:
            with self.subTest(stage=stage, count=count):
                repo = build()
                try:
                    before = self.files()
                    hook = helpers.FaultAt(stage, count=count)
                    with mock.patch.object(fsutil, "_fault_hook", hook), self.assertRaises(BvcError):
                        repo.goto("0")
                    self.assertEqual(self.files(), before)
                    self.assert_clean_at(repo, 2)
                    repo.goto("0")  # やり直せる
                    self.assertEqual(self.files(), {"a.bin": b"a0", "b.bin": b"b0", "d/c.bin": b"c0"})
                finally:
                    repo.close()

    def test_fault_after_swapped_completes_on_next_open(self):
        repo = self.build_linear(1)
        hook = helpers.FaultAt("atomic_write:HEAD.json")
        with mock.patch.object(fsutil, "_fault_hook", hook), self.assertRaises(BvcError):
            repo.undo()
        self.assertTrue((repo.bvc_dir / "journal.json").exists())
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v0")
        with self.assertLogs("bvc.worktree", "WARNING"):
            repo = self.reopen(repo)
        self.assert_clean_at(repo, 0)

    def test_i19_restored_files_are_rehashed_next_time(self):
        self.write("a.bin", b"1", )
        self.write("b.bin", b"b")
        repo = self.init(["**/*.bin"])
        self.write("a.bin", b"2")
        repo.commit()
        repo.undo()
        index = json.loads((repo.bvc_dir / "index.json").read_text("utf-8"))
        ent = index["entries"]["a.bin"]
        self.assertEqual(ent["manifest"], repo._history.get(0).tree["a.bin"])
        # 復元したファイルは stat キャッシュの条件(mtime < fs_time - 2秒)を満たさない
        self.assertGreaterEqual(ent["mtime_ns"], index["fs_time_ns"] - worktree.FS_TIME_MARGIN_NS)
        calls = []
        orig = repo._store.hash_file
        with mock.patch.object(repo._store, "hash_file", lambda f, *a: calls.append(f.name) or orig(f, *a)):
            self.assertFalse(repo.work_state().dirty)
        self.assertTrue(any(c.endswith("a.bin") for c in calls))

    def test_r9_rewrite_keeping_size_and_mtime_is_saved(self):
        self.write("a.bin", b"v0")
        repo = self.init(["**/*.bin"])
        self.write("a.bin", b"v1")
        old = time.time_ns() - 10_000_000_000
        os.utime(self.tmp / "a.bin", ns=(old, old))
        repo.commit()
        repo.commit()  # index を更新して stat キャッシュが効く状態にする
        self.write("a.bin", b"XX")
        os.utime(self.tmp / "a.bin", ns=(old, old))
        self.assertFalse(repo.work_state().dirty)  # S-5: stat だけでは検出できない
        r = repo.undo()
        self.assertIsNotNone(r.auto_commit)
        repo.redo()
        repo.redo()
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"XX")

    def test_r10_untracked_collision(self):
        self.write("a.bin", b"0")
        repo = self.init(["**/*.bin"])
        self.write("b.bin", b"b")
        self.write("d/c.bin", b"c")
        repo.commit()
        repo.undo()
        # 追跡パターンから外したファイル、フォルダ、途中がファイル
        repo.close()
        cfg = json.loads((self.tmp / ".bvc" / "config.json").read_text("utf-8"))
        cfg["track"] = ["a.bin", "d/**"]
        (self.tmp / ".bvc" / "config.json").write_text(json.dumps(cfg), "utf-8")
        repo = self.reopen()
        self.write("b.bin", b"precious")
        with self.assertRaises(SafetyAbort) as cm:
            repo.redo()
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertEqual((self.tmp / "b.bin").read_bytes(), b"precious")
        self.assertEqual(self.head(repo), Head(0, 0))
        self.assertEqual(len(commit_files(repo.bvc_dir)), 2)
        (self.tmp / "b.bin").unlink()
        (self.tmp / "b.bin").mkdir()
        with self.assertRaises(SafetyAbort):
            repo.redo()
        (self.tmp / "b.bin").rmdir()
        (self.tmp / "d").rmdir()
        self.write("d", b"file")
        with self.assertRaises(SafetyAbort):
            repo.redo()
        self.assertEqual((self.tmp / "d").read_bytes(), b"file")
        (self.tmp / "d").unlink()
        repo.redo()
        self.assertEqual((self.tmp / "d" / "c.bin").read_bytes(), b"c")

    def test_tracked_file_replaced_by_folder(self):
        # 追跡ファイル x.bin → フォルダ x.bin/y.bin の入れ替え(先に削除する)
        self.write("x.bin", b"file")
        repo = self.init(["**/*.bin"])
        (self.tmp / "x.bin").unlink()
        self.write("x.bin/y.bin", b"y")
        repo.commit(allow_missing=True)
        # 逆向き(フォルダ → ファイル)は、残るフォルダと衝突するので中止する
        with self.assertRaises(SafetyAbort):
            repo.goto("0")
        # 版 0 の状態を手で作って(undo の代わり)、ファイル → フォルダの向きに移動する
        helpers.remove_tree(self.tmp / "x.bin")
        self.write("x.bin", b"file")
        repo._history.set_head(Head(0, 0))
        repo.goto("1")
        self.assertEqual(self.files(), {"x.bin/y.bin": b"y"})

    def test_r11_change_between_check_and_stash(self):
        for stage in ("stage:0", "replace:stash:0"):
            with self.subTest(stage=stage):
                helpers.remove_tree(self.tmp)
                self.tmp.mkdir()
                repo = self.build_linear(1)
                self.write("a.bin", b"edit")

                def touch(s, stage=stage):
                    if s == stage:
                        with open(self.tmp / "a.bin", "ab") as f:
                            f.write(b"+late")

                with mock.patch.object(fsutil, "_fault_hook", touch), self.assertRaises(FileChanging):
                    repo.undo()
                self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit+late")
                self.assertEqual(self.head(repo).at, 2)
                self.assertFalse((repo.bvc_dir / "journal.json").exists())
                self.assertTrue(repo.work_state().dirty)
                repo.close()

    def test_r6_disk_full(self):
        repo = self.build_linear(1)
        usage = shutil.disk_usage(self.tmp)._replace(free=10)
        with mock.patch.object(worktree.shutil, "disk_usage", return_value=usage), \
                self.assertRaises(DiskFull) as cm:
            repo.undo()
        self.assertEqual(cm.exception.exit_code, 3)
        self.assert_clean_at(repo, 1)
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v1")

    @unittest.skipUnless(os.name == "nt", "Windows の排他(開いているファイルは置き換えられない)")
    def test_r4_file_in_use(self):
        self.write("a.bin", b"a0")
        self.write("b.bin", b"b0")
        repo = self.init(["**/*.bin"])
        self.write("a.bin", b"a1")
        self.write("b.bin", b"b1")
        repo.commit()
        with open(self.tmp / "b.bin", "rb"):
            with self.assertRaises(FileBusy) as cm:
                repo.undo()
            self.assertEqual(cm.exception.exit_code, 3)
        self.assertEqual(self.files(), {"a.bin": b"a1", "b.bin": b"b1"})
        self.assert_clean_at(repo, 1)
        repo.undo()
        self.assertEqual(self.files(), {"a.bin": b"a0", "b.bin": b"b0"})


if __name__ == "__main__":
    unittest.main()
