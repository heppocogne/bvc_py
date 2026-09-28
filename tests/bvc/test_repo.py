# repo の単体テスト(M2-2, M2-10, M3-6, M3-7, M4-1〜M4-11)。
# 観点: F-1, F-2, F-3, F-4, F-6, F-7, F-8, F-9, F-12, F-14, P-5, P-7, P-8, P-9,
#       R-1, R-2, R-4, R-6, R-7, R-8, R-9, R-10, R-11, C-1〜C-8, C-10, C-11。

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
    PinnedCommit,
    RevisionError,
    SafetyAbort,
    UnsupportedFormat,
    UsageError,
)
from bvc.model import BranchInfo, Head
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
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()),
            [".bvc", "a.bin", "note.txt", "sub"],
        )
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
        for kw in (
            {"track": []},
            {"track": ["*"], "compression": "lzma"},
            {"track": ["*"], "chunker": {"name": "fixed", "size": 0}},
        ):
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
        for patch in (
            {"track": "x"},
            {"ignore": [1]},
            {"chunker": {"name": "nope"}},
            {"compression": "x"},
            {"threads": -1},
            {"commit_verify": "x"},
            {"rules": [{"chunker": {"name": "whole"}}]},
            {"rules": [{"pattern": "*", "compression": "x"}]},
            {"rename_threshold": 0},
            {"rename_threshold": 1.5},
            {"rename_threshold": "0.5"},
            {"rename_threshold": True},
        ):
            with self.subTest(patch=patch), self.assertRaises(UsageError):
                parse_config({**base, **patch})
        self.assertEqual(parse_config(base).chunker["name"], "fixed")
        self.assertEqual(parse_config(base).rename_threshold, 0.5)
        self.assertEqual(
            parse_config({**base, "rename_threshold": 1}).rename_threshold, 1.0
        )

    def test_git_config(self):
        # M6: git の設定。bvc.lock が追跡パターンに一致するのは、git 連携が有効なときだけエラー
        base = {"format": 1, "track": ["*.bin"]}
        for git in (
            "x",
            {"enabled": 1},
            {"enabled": True, "lock_file": "../bvc.lock"},
            {"enabled": True, "lock_file": ".bvc/x"},
            {"enabled": True, "lock_file": ""},
            {"enabled": True, "pre_commit": "x"},
            {"enabled": True, "lock_file": "a.bin"},
        ):
            with self.subTest(git=git), self.assertRaises(UsageError):
                parse_config({**base, "git": git})
        self.assertFalse(parse_config(base).git.enabled)
        self.assertEqual(
            parse_config({"format": 1, "track": ["*"]}).git.lock_file, "bvc.lock"
        )
        g = parse_config(
            {
                **base,
                "git": {
                    "enabled": True,
                    "lock_file": "sub/x.lock",
                    "pre_commit": "reject",
                },
            }
        ).git
        self.assertEqual(
            (g.enabled, g.lock_file, g.pre_commit), (True, "sub/x.lock", "reject")
        )
        with self.assertRaises(UsageError):
            parse_config({"format": 1, "track": ["*"], "git": {"enabled": True}})
        parse_config(
            {
                "format": 1,
                "track": ["*"],
                "ignore": ["bvc.lock"],
                "git": {"enabled": True},
            }
        )


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
        with self.assertRaises(MissingFiles), Repo.open(self.tmp) as repo:
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
    STAGES = [  # noqa: RUF012
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
                with (
                    mock.patch.object(fsutil, "_fault_hook", hook),
                    self.assertRaises(Exception),  # noqa: B017
                ):
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
        self.assertEqual(
            (ops[2]["reason"], ops[2]["after"], ops[2]["created"]),
            ("確認", {"at": 0, "branch": 0}, []),
        )

    def test_f12_undo_at_root_and_redo_at_tip(self):
        repo = self.build_linear(1)
        before = self.files()
        with self.assertRaises(CannotMove) as cm:
            repo.redo()
        self.assertEqual(cm.exception.exit_code, 4)
        repo.undo()
        self.write(
            "a.bin", b"edit"
        )  # 移動できない場合は、未コミットの変更があっても何も作らない
        with self.assertRaises(CannotMove):
            repo.undo()
        self.assertEqual(len(commit_files(repo.bvc_dir)), 2)
        self.assertEqual(self.head(repo), Head(0, 0))
        self.assertNotEqual(before, self.files())
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")

    def test_f3_roundtrip_with_branch(self):
        repo = self.build_linear(2)  # 0 - 1 - 2(ブランチ 0)
        repo.undo()  # @ = 1
        self.write("a.bin", b"v3")
        r = repo.commit("c3")  # 1 - 3(ブランチ 1)
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
        self.assertEqual(
            (auto.kind, auto.message, auto.parent), ("auto", "auto: before undo", 1)
        )
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
        repo.undo()  # @ = 1(子 2 がある)
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
        repo.commit("x")  # 0 の子は 1 と 2
        repo._history.set_head(Head(0, 99))  # 経路上に無い状態を作る
        self.write("a.bin", b"v0")
        with self.assertRaises(CannotMove) as cm:
            repo.redo()
        self.assertEqual(cm.exception.details["candidates"], [1, 2])

    def test_p7_case_only_rename_roundtrip(self):
        self.write("a.bin", b"1")
        repo = self.init(["**/*.bin"])
        (self.tmp / "a.bin").rename(self.tmp / "A.bin")
        r = repo.commit()
        self.assertEqual(r.commit.renames, (("a.bin", "A.bin", 1.0),))
        repo.undo()
        self.assertEqual(sorted(os.listdir(self.tmp)), [".bvc", "a.bin"])
        repo.redo()
        self.assertEqual(sorted(os.listdir(self.tmp)), [".bvc", "A.bin"])
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
        with (
            mock.patch.object(fsutil, "_fault_hook", hook),
            self.assertRaises(FileBusy) as cm,
        ):
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
                    with (
                        mock.patch.object(fsutil, "_fault_hook", hook),
                        self.assertRaises(BvcError),
                    ):
                        repo.goto("0")
                    self.assertEqual(self.files(), before)
                    self.assert_clean_at(repo, 2)
                    repo.goto("0")  # やり直せる
                    self.assertEqual(
                        self.files(), {"a.bin": b"a0", "b.bin": b"b0", "d/c.bin": b"c0"}
                    )
                finally:
                    repo.close()

    def test_fault_after_swapped_completes_on_next_open(self):
        repo = self.build_linear(1)
        hook = helpers.FaultAt("atomic_write:HEAD.json")
        with (
            mock.patch.object(fsutil, "_fault_hook", hook),
            self.assertRaises(BvcError),
        ):
            repo.undo()
        self.assertTrue((repo.bvc_dir / "journal.json").exists())
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v0")
        with self.assertLogs("bvc.worktree", "WARNING"):
            repo = self.reopen(repo)
        self.assert_clean_at(repo, 0)

    def test_i19_restored_files_are_rehashed_next_time(self):
        self.write(
            "a.bin",
            b"1",
        )
        self.write("b.bin", b"b")
        repo = self.init(["**/*.bin"])
        self.write("a.bin", b"2")
        repo.commit()
        repo.undo()
        index = json.loads((repo.bvc_dir / "index.json").read_text("utf-8"))
        ent = index["entries"]["a.bin"]
        self.assertEqual(ent["manifest"], repo._history.get(0).tree["a.bin"])
        # 復元したファイルは stat キャッシュの条件(mtime < fs_time - 2秒)を満たさない
        self.assertGreaterEqual(
            ent["mtime_ns"], index["fs_time_ns"] - worktree.FS_TIME_MARGIN_NS
        )
        calls = []
        orig = repo._store.hash_file
        with mock.patch.object(
            repo._store, "hash_file", lambda f, *a: calls.append(f.name) or orig(f, *a)
        ):
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

                with (
                    mock.patch.object(fsutil, "_fault_hook", touch),
                    self.assertRaises(FileChanging),
                ):
                    repo.undo()
                self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit+late")
                self.assertEqual(self.head(repo).at, 2)
                self.assertFalse((repo.bvc_dir / "journal.json").exists())
                self.assertTrue(repo.work_state().dirty)
                repo.close()

    def test_r6_disk_full(self):
        repo = self.build_linear(1)
        usage = shutil.disk_usage(self.tmp)._replace(free=10)
        with (
            mock.patch.object(worktree.shutil, "disk_usage", return_value=usage),
            self.assertRaises(DiskFull) as cm,
        ):
            repo.undo()
        self.assertEqual(cm.exception.exit_code, 3)
        self.assert_clean_at(repo, 1)
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v1")

    @unittest.skipUnless(
        os.name == "nt", "Windows の排他(開いているファイルは置き換えられない)"
    )
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


# ---------------------------------------------------------------------------
# M4-A: 履歴操作(note / branch / discard / gc)
# ---------------------------------------------------------------------------


class HistoryOpsTestCase(MoveTestCase):
    def build_branchy(self):
        # 0 ← 1 ← 2(ブランチ 0)、1 ← 3(ブランチ 1)。@ = 3
        repo = self.build_linear(2)
        repo.undo()
        self.write("a.bin", b"v3")
        repo.commit("c3")
        return repo

    def pin(self, repo, commit_id):
        fsutil.append_jsonl(
            repo.bvc_dir / "pins.jsonl",
            {
                "format": 1,
                "time": "t",
                "git": "a" * 40,
                "bvc": commit_id,
                "tree_hash": "b" * 64,
            },
        )

    def notes(self, repo):
        return {
            e.id: [n.text for n in e.notes] for e in repo.log(include_discarded=True)
        }


class TestNote(HistoryOpsTestCase):
    def test_f14_targets_and_order(self):
        repo = self.build_linear(2)
        self.assertEqual(
            repo.note("2").commit_id, 2
        )  # 本文が数字でも版番号と取り違えない
        repo.note("2 回目")
        repo.note("x", rev="1")
        repo.note("y", rev="@-")
        expected = {2: ["2", "2 回目"], 1: ["x", "y"], 0: []}
        self.assertEqual(self.notes(repo), expected)
        self.assertEqual(self.notes(self.reopen(repo)), expected)

    def test_on_auto_and_unreadable_versions(self):
        repo = self.build_linear(1)
        self.write("a.bin", b"edit")
        auto = repo.undo().auto_commit
        repo.note("auto", rev=str(auto.id))
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "1.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        repo.note("broken", rev="1")  # 仕様書 2.9節: 壊れた版にも付けられる
        self.assertEqual(self.notes(repo)[1], ["broken"])
        self.assertEqual(self.notes(repo)[auto.id], ["auto"])

    def test_errors_write_nothing(self):
        repo = self.build_linear(1)
        repo.discard("0")
        for text, rev, exc in (
            ("", "@", UsageError),
            ("x", "9", RevisionError),
            ("x", "0", RevisionError),
        ):
            with self.subTest(text=text, rev=rev), self.assertRaises(exc):
                repo.note(text, rev=rev)
        self.assertEqual(list((repo.bvc_dir / "notes").iterdir()), [])

    def test_c8_broken_line_is_skipped(self):
        repo = self.build_linear(1)
        repo.note("a")
        with open(repo.bvc_dir / "notes" / "1.jsonl", "ab") as f:
            f.write(b"{broken\n")
        repo.note("b")
        with self.assertLogs("bvc.history", "WARNING"):
            self.assertEqual(self.notes(repo)[1], ["a", "b"])


class TestBranchOps(HistoryOpsTestCase):
    def test_f1_list_with_fork_and_current(self):
        repo = self.build_branchy()
        self.assertEqual(
            repo.branches(),
            [BranchInfo(0, None, 2, None, False), BranchInfo(1, None, 3, 1, True)],
        )

    def test_f1_name_and_reassign(self):
        repo = self.build_branchy()
        self.assertEqual(
            repo.name_branch("main", "2"), BranchInfo(0, "main", 2, None, False)
        )
        repo.name_branch("feat")
        repo.goto("main")
        self.assertEqual(self.head(repo), Head(2, 0))
        # 既存の名前は付け替える。ブランチ 1 の古い名前 feat は外れる
        with self.assertLogs("bvc.repo", "INFO") as cm:
            info = repo.name_branch("main", "3")
        self.assertIn("付け替え", cm.output[0])
        self.assertEqual((info.number, info.name), (1, "main"))
        names = {b.number: b.name for b in self.reopen(repo).branches()}
        self.assertEqual(names, {0: None, 1: "main"})

    def test_f1_unname_and_errors(self):
        repo = self.build_branchy()
        repo.name_branch("feat")
        self.assertEqual(repo.unname_branch("feat").name, None)
        with self.assertRaises(RevisionError):
            repo.unname_branch("feat")
        for bad in ("1", "a b", "x-y", "@x"):
            with self.subTest(name=bad), self.assertRaises(UsageError):
                repo.name_branch(bad)
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "2.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        before = (repo.bvc_dir / "branches.json").read_bytes()
        with self.assertRaises(BvcError):  # 読めない版はブランチが分からない
            repo.name_branch("x", "2")
        self.assertEqual((repo.bvc_dir / "branches.json").read_bytes(), before)

    def test_named_branch_without_living_versions_is_listed(self):
        repo = self.build_branchy()
        repo.name_branch("feat")
        repo.goto("2")
        repo.discard("3")
        self.assertIn(BranchInfo(1, "feat", None, None, False), repo.branches())


class TestDiscard(HistoryOpsTestCase):
    def test_f8_middle(self):
        repo = self.build_linear(3)
        r = repo.discard("1")
        self.assertEqual((r.discarded, r.before, r.after), (1, Head(3, 0), Head(3, 0)))
        self.assertEqual([e.id for e in repo.log()], [3, 2, 0])
        self.assertTrue(
            next(e for e in repo.log(include_discarded=True) if e.id == 1).discarded
        )
        repo.undo()
        repo.undo()
        self.assert_clean_at(repo, 0)
        self.assertEqual(self.files(), {"a.bin": b"v0"})
        repo.redo()
        self.assert_clean_at(repo, 2)

    def test_f8_branch_point(self):
        repo = self.build_branchy()
        repo.discard("1")
        self.assertEqual(repo._history.children(0), [2, 3])
        self.assertEqual(repo.resolve("@-"), 0)
        repo.undo()
        self.assertEqual(self.files(), {"a.bin": b"v0"})

    def test_f8_root_not_current(self):
        repo = self.build_linear(2)
        repo.discard("0")
        self.assertIsNone(repo._history.effective_parent(1))
        repo.goto("1")
        with self.assertRaises(CannotMove):
            repo.undo()

    def test_f8_current_moves_to_parent(self):
        repo = self.build_linear(2)
        r = repo.discard()
        self.assertEqual(
            (r.after, r.restored, r.auto_commit), (Head(1, 0), ["a.bin"], None)
        )
        self.assert_clean_at(repo, 1)
        self.assertEqual(self.files(), {"a.bin": b"v1"})
        self.assertTrue(repo._history.is_discarded(2))
        with self.assertRaises(CannotMove):  # 2 は消えたので 1 が先端
            repo.redo()
        last = self.oplog(repo)[-1]
        self.assertEqual(
            (last["op"], last["after"], last["result"]),
            ("discard", {"at": 1, "branch": 0}, "ok"),
        )

    def test_f8_current_with_changes_keeps_work(self):
        repo = self.build_linear(2)
        self.write("a.bin", b"edit")
        r = repo.discard()
        auto = r.auto_commit
        self.assertEqual((auto.parent, auto.message), (2, "auto: before discard"))
        self.assertEqual(r.after, Head(1, auto.branch))
        self.assertEqual(
            repo._history.effective_parent(auto.id), 1
        )  # 消した版の子はつなぎ直される
        self.assert_clean_at(repo, 1)
        repo.redo()  # 作業内容(自動コミット)に戻れる
        self.assertEqual(self.files(), {"a.bin": b"edit"})

    def test_f8_current_root_is_cannot_move(self):
        self.write("a.bin", b"v0")
        repo = self.init(["*.bin"])
        self.write("a.bin", b"edit")
        with self.assertRaises(CannotMove) as cm:
            repo.discard()
        self.assertEqual(cm.exception.exit_code, 4)
        self.assertEqual(len(commit_files(repo.bvc_dir)), 1)  # 自動コミットも作らない
        self.assertFalse(repo._history.is_discarded(0))
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"edit")

    def test_f8_consecutive(self):
        repo = self.build_linear(3)
        repo.discard()
        repo.discard()
        self.assert_clean_at(repo, 1)
        self.assertEqual([e.id for e in repo.log()], [1, 0])

    def test_f2_commit_after_discard_extends_branch(self):
        repo = self.build_linear(2)
        repo.discard()
        self.write("a.bin", b"v9")
        r = repo.commit()
        self.assertFalse(r.new_branch)
        self.assertEqual((r.commit.parent, r.commit.branch), (1, 0))

    def test_f7_missing(self):
        repo = self.build_linear(2)
        (self.tmp / "a.bin").unlink()
        with self.assertRaises(MissingFiles):
            repo.discard()
        self.assertFalse(repo._history.is_discarded(2))
        self.assertEqual(self.head(repo), Head(2, 0))
        r = repo.discard(allow_missing=True)
        self.assertEqual(r.after.at, 1)
        self.assertEqual(self.files(), {"a.bin": b"v1"})

    def test_restore_failure_discards_nothing(self):
        repo = self.build_linear(2)
        self.write("a.bin", b"edit")
        hook = helpers.FaultAt("replace:swap:0")
        with (
            mock.patch.object(fsutil, "_fault_hook", hook),
            self.assertRaises(FileBusy),
        ):
            repo.discard()
        self.assertFalse(repo._history.is_discarded(2))
        self.assert_clean_at(repo, 3)  # HEAD は自動コミット(I-18)
        self.assertEqual(self.files(), {"a.bin": b"edit"})
        self.assertFalse(self.reopen(repo)._history.is_discarded(2))

    def test_errors(self):
        repo = self.build_linear(3)
        repo.discard("1")
        with self.assertRaises(RevisionError):
            repo.discard("1")
        with self.assertRaises(RevisionError):
            repo.discard("9")
        self.pin(repo, 2)
        repo = self.reopen(repo)
        with self.assertRaises(PinnedCommit) as cm:
            repo.discard("2")
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertFalse(repo._history.is_discarded(2))
        repo.discard("2", force=True)
        self.assertTrue(repo._history.is_discarded(2))

    def test_unreadable_version(self):
        repo = self.build_linear(2)
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "1.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        repo.discard("1")  # 仕様書 2.9節: 壊れた版にも実行できる
        self.assertEqual([e.id for e in repo.log()], [2, 0])
        self.assertEqual(repo._history.effective_parent(2), 0)


class TestGc(HistoryOpsTestCase):
    def snapshot(self):
        bvc = self.tmp / ".bvc"
        return {
            p.relative_to(bvc).as_posix(): p.read_bytes()
            for p in bvc.rglob("*")
            if p.is_file()
        }

    def build(self):
        # 版 0: v0、1: v1、2: v2、3: v0(版 0 と同じ内容)。1 と 2 を削除済みにする。@ = 3
        repo = self.build_linear(2)
        self.write("a.bin", b"v0")
        repo.commit("c3")
        repo.discard("1")
        repo.discard("2")
        return repo

    def assert_all_restorable(self, repo, expected):
        for cid, data in expected.items():
            repo.goto(str(cid))
            self.assertEqual(self.files(), {"a.bin": data})
        self.assertTrue(repo._store.verify_all().ok)

    def test_f9_dry_run_then_gc(self):
        repo = self.build()
        before = self.snapshot()
        dry = repo.gc(dry_run=True)
        self.assertEqual(self.snapshot(), before)  # 何も変わらない
        self.assertEqual(
            (
                dry.changed,
                dry.deleted_commits,
                dry.deleted_manifests,
                dry.deleted_chunks,
                dry.skipped,
            ),
            (False, [1, 2], 2, 2, []),
        )
        self.assertGreater(dry.freed_bytes, 0)
        r = repo.gc()
        self.assertTrue(r.changed)
        self.assertEqual(
            (r.deleted_commits, r.deleted_manifests, r.deleted_chunks, r.freed_bytes),
            (
                dry.deleted_commits,
                dry.deleted_manifests,
                dry.deleted_chunks,
                dry.freed_bytes,
            ),
        )
        self.assertEqual(sorted(commit_files(repo.bvc_dir)), [0, 3])
        self.assertEqual([e.id for e in repo.log(include_discarded=True)], [3, 0])
        self.assertEqual(self.oplog(repo)[-1]["op"], "gc")
        self.assert_all_restorable(repo, {0: b"v0", 3: b"v0"})
        self.assertFalse(repo.gc().changed)

    def test_f9_shared_data_is_kept(self):
        repo = self.build()
        repo.gc()
        repo.discard("0")  # 版 3 と同じマニフェストを持つ根
        r = repo.gc()
        self.assertEqual(
            (r.deleted_commits, r.deleted_manifests, r.deleted_chunks), ([0], 0, 0)
        )
        self.assert_all_restorable(self.reopen(repo), {3: b"v0"})

    def test_f9_nothing_to_delete(self):
        repo = self.build_linear(1)
        r = repo.gc()
        self.assertEqual(
            (
                r.changed,
                r.deleted_commits,
                r.deleted_manifests,
                r.deleted_chunks,
                r.deleted_tmp,
            ),
            (False, [], 0, 0, 0),
        )
        self.assertNotEqual(self.oplog(repo)[-1]["op"], "gc")

    def test_f9_pinned_version_is_kept(self):
        repo = self.build()
        self.pin(repo, 1)
        repo = self.reopen(repo)
        self.assertEqual(repo.gc().deleted_commits, [2])
        self.assertEqual(sorted(commit_files(repo.bvc_dir)), [0, 1, 3])
        self.assertTrue(
            repo._store.manifest_ok(repo._history.get(1).tree["a.bin"], "full")
        )

    def test_notes_and_tmp(self):
        repo = self.build()
        repo.note("x", rev="3")
        fsutil.append_jsonl(
            repo.bvc_dir / "notes" / "1.jsonl",
            {"format": 1, "time": "t", "text": "old"},
        )
        fsutil.append_jsonl(
            repo.bvc_dir / "notes" / "99.jsonl",
            {"format": 1, "time": "t", "text": "orphan"},
        )
        (repo.bvc_dir / "tmp" / "left.tmp").write_bytes(b"garbage")
        r = repo.gc()
        self.assertEqual(r.deleted_tmp, 1)
        self.assertEqual(
            sorted(p.name for p in (repo.bvc_dir / "notes").iterdir()), ["3.jsonl"]
        )
        self.assertEqual(list((repo.bvc_dir / "tmp").iterdir()), [])

    def test_unknown_tree_keeps_all_data(self):
        # 有効な版が読めないと、参照するデータが分からないので、マニフェスト・チャンクは消さない(D-15)
        repo = self.build()
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "3.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        manifests = sorted(repo._store.iter_manifests())
        chunks = sorted(repo._store.iter_chunks())
        with self.assertLogs("bvc.repo", "WARNING"):
            r = repo.gc()
        self.assertEqual(
            (r.deleted_commits, r.skipped), ([1, 2], ["manifests", "chunks"])
        )
        self.assertEqual(sorted(repo._store.iter_manifests()), manifests)
        self.assertEqual(sorted(repo._store.iter_chunks()), chunks)
        self.assertEqual(
            sorted(p.name for p in (repo.bvc_dir / "commits").iterdir()),
            ["0.json", "3.json"],
        )

    def test_unreadable_manifest_keeps_chunks(self):
        repo = self.build()
        helpers.flip_byte(repo._store.manifest_path(repo._history.get(0).tree["a.bin"]))
        chunks = sorted(repo._store.iter_chunks())
        with self.assertLogs("bvc.repo", "WARNING"):
            r = repo.gc()
        self.assertEqual((r.deleted_manifests, r.skipped), (2, ["chunks"]))
        self.assertEqual(sorted(repo._store.iter_chunks()), chunks)

    def test_r8_fault_during_gc(self):
        # 途中で止まっても有効な版はすべて復元でき、もう一度 gc すれば完了する
        stages = [
            ("gc:commit", 1),
            ("gc:commit", 2),
            ("gc:note", 1),
            ("gc:manifest", 1),
            ("gc:manifest", 2),
            ("gc:chunk", 1),
            ("gc:chunk", 2),
            ("gc:tmp", 1),
        ]
        for stage, count in stages:
            with self.subTest(stage=stage, count=count):
                helpers.remove_tree(self.tmp)
                self.tmp.mkdir()
                repo = self.build()
                fsutil.append_jsonl(
                    repo.bvc_dir / "notes" / "99.jsonl",
                    {"format": 1, "time": "t", "text": "x"},
                )
                (repo.bvc_dir / "tmp" / "left.tmp").write_bytes(b"garbage")
                hook = helpers.FaultAt(stage, count=count)
                with (
                    mock.patch.object(fsutil, "_fault_hook", hook),
                    self.assertRaises(OSError),
                ):
                    repo.gc()
                repo = self.reopen(repo)
                self.assert_all_restorable(repo, {0: b"v0", 3: b"v0"})
                self.assertTrue(repo.gc().changed)
                self.assertEqual(sorted(commit_files(repo.bvc_dir)), [0, 3])
                self.assert_all_restorable(repo, {0: b"v0", 3: b"v0"})
                repo.close()


class TestRenameDetection(RepoTestCase):
    # M4-5, M4-6: 名前変更の検知(F-6、設計書 4.3節、仕様書 2.6節)。
    # 1KiB の固定長で分割し、共通チャンクの割合(新ファイル基準)を狙った値にする。

    K = 1024

    def init(self, track=("*.bin",), **kw):
        return super().init(track, chunker={"name": "fixed", "size": self.K}, **kw)

    def blocks(self, *names: str) -> bytes:
        # 1文字ごとに 1KiB のブロックを作る(同じ文字は同じチャンクになる)
        return b"".join(n.encode() * self.K for n in names)

    def set_config(self, repo, **values):
        path = self.tmp / ".bvc" / "config.json"
        cfg = json.loads(path.read_text("utf-8"))
        cfg.update(values)
        path.write_text(json.dumps(cfg), "utf-8")
        return self.reopen(repo)

    def test_f6_exact_match(self):
        self.write("a.bin", b"content1")
        self.write("b.bin", b"content2")
        repo = self.init()
        (self.tmp / "a.bin").rename(self.tmp / "c.bin")
        r = repo.commit()
        self.assertEqual(
            (r.state.renamed, r.state.added, r.state.missing),
            ([("a.bin", "c.bin", 1.0)], [], []),
        )
        self.assertEqual(r.commit.renames, (("a.bin", "c.bin", 1.0),))

    def test_f6_exact_match_with_different_chunker(self):
        # 分割方式が変わってマニフェストが別物でも、内容が同じなら完全一致とみなす
        self.write("a.bin", self.blocks("x", "y", "z"))
        repo = self.init()
        repo = self.set_config(
            repo, rules=[{"pattern": "b.bin", "chunker": {"name": "whole"}}]
        )
        (self.tmp / "a.bin").rename(self.tmp / "b.bin")
        r = repo.commit()
        self.assertNotEqual(r.commit.tree["b.bin"], repo.get_commit(0).tree["a.bin"])
        self.assertEqual(r.state.renamed, [("a.bin", "b.bin", 1.0)])

    def test_f6_same_content_paired_in_path_order(self):
        # 同一内容のファイルが複数あれば、パスの辞書順で1対1に対応付ける
        self.write("g1.bin", b"same")
        self.write("g2.bin", b"same")
        repo = self.init()
        for g in ("g1.bin", "g2.bin"):
            (self.tmp / g).unlink()
        self.write("n2.bin", b"same")
        self.write("n1.bin", b"same")
        r = repo.commit()
        self.assertEqual(
            r.state.renamed, [("g1.bin", "n1.bin", 1.0), ("g2.bin", "n2.bin", 1.0)]
        )

    def test_f6_similar_above_and_below_threshold(self):
        # 共通 3/4 = 0.75(既定 0.5 以上)と、共通 1/4 = 0.25(未満)
        self.write("a.bin", self.blocks("x", "x", "x", "a"))
        self.write("b.bin", self.blocks("y", "b", "b", "b"))
        repo = self.init()
        (self.tmp / "a.bin").unlink()
        (self.tmp / "b.bin").unlink()
        self.write("a2.bin", self.blocks("x", "x", "x", "c"))
        self.write("b2.bin", self.blocks("y", "d", "d", "d"))
        with self.assertRaises(MissingFiles) as cm:
            repo.commit()
        self.assertEqual(cm.exception.details["missing"], ["b.bin"])
        r = repo.commit(allow_missing=True)
        self.assertEqual(r.state.renamed, [("a.bin", "a2.bin", 0.75)])
        self.assertEqual((r.state.added, r.state.missing), (["b2.bin"], ["b.bin"]))

    def test_f6_threshold_boundary_and_config(self):
        # sim = しきい値ちょうどは名前変更。しきい値は config.json の rename_threshold を使う
        self.write("a.bin", self.blocks("x", "a"))
        repo = self.init()
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", self.blocks("x", "b"))
        self.assertEqual(repo.work_state().renamed, [("a.bin", "b.bin", 0.5)])
        repo = self.set_config(repo, rename_threshold=0.6)
        self.assertEqual(repo.work_state().missing, ["a.bin"])

    def test_f6_similarity_uses_new_file_size(self):
        # sim は新ファイル基準: 追記で大きくなったファイルは割合が下がる
        self.write("a.bin", self.blocks("x", "y"))
        repo = self.init()
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", self.blocks("x", "y", "p", "q", "r"))  # 2/5 = 0.4
        self.assertEqual(repo.work_state().missing, ["a.bin"])

    def test_f6_greedy_by_similarity_then_path(self):
        # sim の高い組から確定する。同じ sim なら辞書順で、実行ごとに変わらない
        self.write("g1.bin", self.blocks("c", "1"))
        self.write("g2.bin", self.blocks("c", "2"))
        self.write("h.bin", self.blocks("h", "h", "h", "0"))
        repo = self.init()
        for g in ("g1.bin", "g2.bin", "h.bin"):
            (self.tmp / g).unlink()
        self.write("n2.bin", self.blocks("c", "4"))
        self.write("n1.bin", self.blocks("c", "3"))
        self.write(
            "m.bin", self.blocks("h", "h", "h", "c")
        )  # h.bin と 0.75、g1/g2 と 0.25
        r = repo.commit()
        self.assertEqual(
            r.state.renamed,
            [
                ("h.bin", "m.bin", 0.75),
                ("g1.bin", "n1.bin", 0.5),
                ("g2.bin", "n2.bin", 0.5),
            ],
        )

    def test_f6_manual_rename_takes_priority(self):
        # 自動なら a → b(完全一致)になる場面で、手動指定の a → c を優先する
        self.write("a.bin", b"content_a")
        repo = self.init()
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", b"content_a")
        self.write("c.bin", b"other")
        self.assertEqual(repo.work_state().renamed, [("a.bin", "b.bin", 1.0)])
        r = repo.commit(renames=[("a.bin", "c.bin")])
        self.assertEqual(
            (r.state.renamed, r.state.added), ([("a.bin", "c.bin", 1.0)], ["b.bin"])
        )

    def test_f6_manual_rename_path_forms(self):
        # '\\' 区切り・先頭の './'・大文字小文字の違いを受け付ける
        self.write("sub/a.bin", b"aaa")
        repo = self.init(track=["**/*.bin"])
        (self.tmp / "sub" / "a.bin").unlink()
        self.write("sub/z.bin", b"zzz")
        r = repo.commit(renames=[(".\\sub\\A.bin", "./sub/z.bin")])
        self.assertEqual(r.state.renamed, [("sub/a.bin", "sub/z.bin", 1.0)])

    def test_f6_manual_rename_invalid_is_usage_error(self):
        # 当てはまらない・重複・不正なパスの指定は、何も記録せずに中止する(D-15)
        self.write("a.bin", b"a")
        self.write("b.bin", b"b")
        repo = self.init()
        (self.tmp / "a.bin").unlink()
        (self.tmp / "b.bin").unlink()
        self.write("c.bin", b"c")
        self.write("d.bin", b"d")
        for renames in (
            [("x.bin", "c.bin")],  # 消えていない
            [("a.bin", "b.bin")],  # 新しいファイルではない
            [("a.bin", "c.bin"), ("b.bin", "c.bin")],  # 新しい側の重複
            [("../a.bin", "c.bin")],  # 作業フォルダの外
            [(".bvc/x", "c.bin")],
        ):
            with self.subTest(renames=renames), self.assertRaises(UsageError):
                repo.commit(renames=renames)
        self.assertEqual(len(commit_files(repo.bvc_dir)), 1)

    def test_f6_hint_for_file_moved_out_of_pattern(self):
        # 仕様書 2.6節の例: result.bin → result.bin.tmp。rules で分割方式を変えていても見つける
        self.write("result.bin", self.blocks("r", "s", "t"))
        self.write("sub/x.bin", b"xx")
        repo = self.init(track=["*.bin", "sub/*.bin"])
        repo = self.set_config(
            repo, rules=[{"pattern": "*.bin", "chunker": {"name": "whole"}}]
        )
        (self.tmp / "result.bin").rename(self.tmp / "result.bin.tmp")
        (self.tmp / "sub" / "x.bin").rename(self.tmp / "sub" / "x.bak")
        self.write("other.tmp", self.blocks("r", "s", "u"))  # 同じサイズで内容が違う
        with self.assertRaises(MissingFiles) as cm:
            repo.commit()
        e = cm.exception
        self.assertEqual(
            e.details["hints"],
            {"result.bin": ["result.bin.tmp"], "sub/x.bin": ["sub/x.bak"]},
        )
        self.assertIn(
            "  missing: result.bin\n  ヒント: パターン外に同じ内容のファイルがあります: result.bin.tmp",
            str(e),
        )
        self.assertEqual(repo.work_state().hints, e.details["hints"])
        # --allow-missing のときは探さない(読み込みを省く)
        r = repo.commit(allow_missing=True)
        self.assertEqual(
            (r.state.missing, r.state.hints), (["result.bin", "sub/x.bin"], {})
        )

    def test_f6_status_has_no_side_effects_and_matches_commit(self):
        # status は保存しないため、新ファイルのマニフェストが無くても隔離記録を作らない
        self.write("a.bin", self.blocks("x", "x", "x", "a"))
        repo = self.init()
        health = (self.tmp / ".bvc" / "health.json").read_bytes()
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", self.blocks("x", "x", "x", "b"))
        s = repo.work_state()
        self.assertEqual(
            (s.renamed, s.added, s.missing), ([("a.bin", "b.bin", 0.75)], [], [])
        )
        self.assertEqual((self.tmp / ".bvc" / "health.json").read_bytes(), health)
        self.assertFalse(list((self.tmp / ".bvc" / "quarantine").rglob("*.json")))
        self.assertEqual(repo.commit().state.renamed, s.renamed)

    def test_f6_auto_commit_detects_similar_rename(self):
        # 自動コミットも commit と同じ判定を使う
        repo = self.init()
        self.write("a.bin", self.blocks("x", "x", "x", "a"))
        repo.commit()
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", self.blocks("x", "x", "x", "b"))
        r = repo.goto("0")
        self.assertEqual(r.auto_commit.renames, (("a.bin", "b.bin", 0.75),))


# ---------------------------------------------------------------------------
# M4-C 破損時の処理(M4-7〜M4-11)
# ---------------------------------------------------------------------------

# チャンクファイルの壊し方(設計書 7節)。圧縮なし(codec 0)のチャンクに使う
DAMAGES = {
    "delete": lambda p: p.unlink(),
    "flip": helpers.flip_byte,
    "truncate": lambda p: helpers.truncate_file(p, 1),  # ヘッダだけ残す
    "codec": lambda p: helpers.set_first_byte(
        p, 1
    ),  # raw → zlib(既知の ID で復号できない)
}


class CorruptionTestCase(MoveTestCase):
    def manifest_of(self, repo, cid, rel="a.bin"):
        return repo._history.get(cid).tree[rel]

    def chunk_of(self, repo, cid, rel="a.bin"):
        # 版 cid の rel の最初のチャンクのファイル
        m = repo._store.get_manifest(self.manifest_of(repo, cid, rel))
        return repo._store.chunk_path(m.chunks[0].sha)

    def set_config(self, repo, **values):
        path = repo.bvc_dir / "config.json"
        cfg = json.loads(path.read_text("utf-8"))
        cfg.update(values)
        path.write_text(json.dumps(cfg), "utf-8")
        return self.reopen(repo)

    def make_old(self, rel):
        # stat キャッシュが使われるように、更新日時を十分に古くする(設計書 4.2節)
        t = time.time() - 100
        os.utime(self.tmp / rel, (t, t))

    def quarantined(self, repo, kind="chunks"):
        return sorted(p.name for p in (repo.bvc_dir / "quarantine" / kind).glob("*"))


class TestCommitVerify(CorruptionTestCase):
    # M4-7: コミット時の検査(C-1, C-10、設計書 4.11節)。

    def build(self, commit_verify):
        # 版 0: a.bin(変更しない。stat キャッシュを通る)と b.bin
        self.write("a.bin", b"A" * 100)
        self.write("b.bin", b"B0")
        self.make_old("a.bin")
        repo = self.init(["*.bin"], compression="none")
        repo = self.set_config(repo, commit_verify=commit_verify)
        self.assertEqual(repo.config.commit_verify, commit_verify)
        return repo

    def assert_restorable(self, repo, expected):
        for cid, files in expected.items():
            repo.goto(str(cid))
            self.assertEqual(self.files(), files)
        self.assertTrue(repo.verify().ok)

    def test_c1_damaged_chunk_of_unchanged_file_is_rebuilt_with_full(self):
        for name, damage in DAMAGES.items():
            with self.subTest(damage=name):
                helpers.remove_tree(self.tmp)
                self.tmp.mkdir()
                repo = self.build("full")
                damage(self.chunk_of(repo, 0))
                self.write("b.bin", b"B1")
                r = repo.commit("edit")
                # 新しい版は壊れたデータを参照しない。同じ内容なので、壊れていた版 0 も直る
                self.assertEqual(r.state.modified, ["b.bin"])
                self.assert_restorable(
                    repo,
                    {
                        0: {"a.bin": b"A" * 100, "b.bin": b"B0"},
                        1: {"a.bin": b"A" * 100, "b.bin": b"B1"},
                    },
                )
                repo.close()

    def test_c1_missing_or_quarantined_chunk_is_rebuilt_with_exists(self):
        # exists でも、欠損と隔離済み(以前に検出したもの)は作り直す
        repo = self.build("exists")
        self.chunk_of(repo, 0).unlink()
        self.write("b.bin", b"B1")
        repo.commit("1")
        self.assertTrue(repo._store.has_chunk(self.chunk_of(repo, 1).name, "full"))

        helpers.flip_byte(self.chunk_of(repo, 1))
        self.assertFalse(repo.verify().ok)  # 検出して隔離する
        self.write("b.bin", b"B2")
        repo.commit("2")
        self.assert_restorable(
            repo,
            {
                1: {"a.bin": b"A" * 100, "b.bin": b"B1"},
                2: {"a.bin": b"A" * 100, "b.bin": b"B2"},
            },
        )

    def test_c1_changed_file_rebuilds_its_chunks(self):
        # 変更のあったファイルも、保存済みのチャンクが欠けていれば作り直す(put_file の has_chunk)
        repo = self.build("exists")
        self.write("b.bin", b"B1")
        repo.commit()
        victim = self.chunk_of(repo, 0, "b.bin")
        victim.unlink()
        self.write("b.bin", b"B0")  # 版 0 と同じ内容
        repo.commit()
        self.assertTrue(victim.exists())
        self.assert_restorable(repo, {0: {"a.bin": b"A" * 100, "b.bin": b"B0"}})

    def test_c10_exists_misses_latent_corruption(self):
        # exists は中身を読まないので、ビット化けは見逃す(既定値の限界)。full と verify では検出する
        repo = self.build("exists")
        victim = self.chunk_of(repo, 0)
        helpers.flip_byte(victim)
        self.write("b.bin", b"B1")
        repo.commit()
        self.assertTrue(
            victim.exists()
        )  # 見逃して、新しい版が壊れたチャンクを参照している
        self.assertEqual(repo.verify(quick=True).broken_commits, [])
        self.assertEqual(repo.verify().broken_commits, [0, 1])
        # 検出後の commit は作り直す
        self.write("b.bin", b"B2")
        repo.commit()
        self.assertEqual(repo.verify().broken_commits, [])

        repo = self.set_config(repo, commit_verify="full")
        helpers.flip_byte(self.chunk_of(repo, 2))
        self.write("b.bin", b"B3")
        repo.commit()
        # 同じチャンクを2回隔離した(2回目は別名で残す)
        self.assertEqual([n[:64] for n in self.quarantined(repo)], [victim.name] * 2)
        self.assertTrue(repo.verify().ok)


class TestSkipBroken(CorruptionTestCase):
    # M4-8: 壊れた版への移動(C-2、仕様書 3.4節、設計書 4.11節)。

    def build(self):
        # 版 0..7(a.bin = v<番号>)。4 と 6 を削除して 7 ← 5 ← 3 にし、版 5 のマニフェストを壊す。@ = 7
        repo = self.build_linear(7)
        repo.discard("4")
        repo.discard("6")
        helpers.flip_byte(repo._store.manifest_path(self.manifest_of(repo, 5)))
        return repo

    def test_c2_undo_redo_goto(self):
        repo = self.build()
        n_commits = len(commit_files(repo.bvc_dir))
        self.write("a.bin", b"edit")
        with self.assertRaises(BrokenVersion) as cm:
            repo.undo()
        self.assertIn("--skip-broken", str(cm.exception))
        # 作業ファイルも HEAD も変えず、自動コミットも作らない
        self.assertEqual(
            (self.head(repo), self.files()), (Head(7, 0), {"a.bin": b"edit"})
        )
        self.assertEqual(len(commit_files(repo.bvc_dir)), n_commits)

        self.write("a.bin", b"v7")
        r = repo.undo(skip_broken=True)
        self.assertEqual((r.after, r.skipped), (Head(3, 0), [5]))
        self.assertEqual(self.files(), {"a.bin": b"v3"})
        self.assertEqual(self.oplog(repo)[-1]["args"]["skipped"], [5])

        with self.assertRaises(BrokenVersion):
            repo.redo()
        self.assertEqual(self.files(), {"a.bin": b"v3"})
        r = repo.redo(skip_broken=True)
        self.assertEqual((r.after, r.skipped), (Head(7, 0), [5]))
        self.assertEqual(self.files(), {"a.bin": b"v7"})

        with self.assertRaises(BrokenVersion):
            repo.goto("5")
        repo.goto("3")  # 途中の版が壊れていても、健全な版へは直接移動できる
        self.assert_clean_at(repo, 3)

    def test_skip_with_auto_commit(self):
        repo = self.build()
        self.write("a.bin", b"edit")
        r = repo.undo(skip_broken=True)
        self.assertEqual((r.auto_commit.id, r.after.at, r.skipped), (8, 3, [5]))
        # redo で壊れた版を飛ばして、編集内容(自動コミット)へ戻れる
        self.assertEqual(repo.redo(skip_broken=True).after.at, 7)
        repo.redo()
        self.assertEqual(self.files(), {"a.bin": b"edit"})

    def test_no_healthy_version_in_the_direction(self):
        repo = self.build_linear(2)
        for cid in (0, 1):
            helpers.flip_byte(repo._store.manifest_path(self.manifest_of(repo, cid)))
        with self.assertRaises(BrokenVersion) as cm:
            repo.undo(skip_broken=True)
        self.assertIn("健全な版がありません", str(cm.exception))
        self.assert_clean_at(repo, 2)
        repo.goto("2")
        with self.assertRaises(
            CannotMove
        ):  # 根での undo は skip_broken でも終了コード 4
            repo._history.set_head(Head(0, 0))
            repo.undo(skip_broken=True)

    def test_unreadable_version_is_skipped(self):
        repo = self.build_linear(3)
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "2.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        with self.assertRaises(BrokenVersion):
            repo.undo()
        self.assertEqual(repo.undo(skip_broken=True).skipped, [2])
        self.assert_clean_at(repo, 1)
        # 経路の外(読み込み不可の版のブランチは分からない)でも、子が1つなら飛ばして進める
        self.assertEqual(repo.redo(skip_broken=True).after.at, 3)


class TestBrokenHead(CorruptionTestCase):
    # 現在位置(@)の版自体が壊れている場合(C-12)。作業内容を新しい版に保存でき、他の版へ移動できる。

    def build(self, broken_tree=False):
        # 版 0..3(a.bin = v<番号>)。@ = 3 の版ファイルを壊す(broken_tree なら tree に不正なパスを入れる)
        repo = self.build_linear(3)
        repo.close()
        path = self.tmp / ".bvc" / "commits" / "3.json"
        if broken_tree:
            data = json.loads(path.read_text("utf-8"))
            data["tree"]["../x.bin"] = data["tree"]["a.bin"]
            path.write_text(json.dumps(data), "utf-8")
        else:
            helpers.break_json(path)
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        self.assertEqual(self.head(repo).at, 3)
        return repo

    def test_c12_commit_from_unreadable_head(self):
        repo = self.build()
        self.write("a.bin", b"edit")
        with self.assertLogs("bvc.repo", "WARNING"):
            r = repo.commit("save")
        self.assertTrue(r.changed)
        self.assertEqual((r.commit.id, r.commit.parent), (4, 3))
        # 版 3 の祖先は操作ログから補う(版 2 以前とのつながりを保つ)
        self.assertEqual(r.commit.ancestors, (3, 2, 1, 0))
        self.assertEqual(r.state.added, ["a.bin"])
        self.assert_clean_at(repo, 4)
        self.assertEqual(repo.undo(skip_broken=True).after.at, 2)
        self.assertEqual(self.files(), {"a.bin": b"v2"})
        repo.goto("4")
        self.assertEqual(self.files(), {"a.bin": b"edit"})

    def test_c12_commit_without_edit_leaves_broken_head(self):
        # 内容が変わっていなくても、壊れた版に留まらないよう新しい版を作る
        repo = self.build()
        with self.assertLogs("bvc.repo", "WARNING"):
            r = repo.commit()
        self.assertTrue(r.changed)
        self.assert_clean_at(repo, 4)
        self.assertEqual(repo.commit().changed, False)

    def test_c12_move_from_unreadable_head_auto_commits(self):
        repo = self.build()
        self.write("a.bin", b"edit")
        with self.assertLogs("bvc.repo", "WARNING"):
            r = repo.undo()
        self.assertEqual((r.auto_commit.id, r.auto_commit.parent), (4, 3))
        self.assert_clean_at(repo, 2)
        self.assertEqual(self.files(), {"a.bin": b"v2"})
        repo.goto("4")
        self.assertEqual(self.files(), {"a.bin": b"edit"})

    def test_c12_move_without_edit_from_broken_tree_head(self):
        # tree に不正な値がある版からも、作業内容を自動コミットしてから移動する
        repo = self.build(broken_tree=True)
        with self.assertLogs("bvc.repo", "WARNING"):
            r = repo.goto("1")
        self.assertEqual(r.auto_commit.id, 4)
        self.assertEqual(sorted(r.auto_commit.tree), ["a.bin"])
        self.assert_clean_at(repo, 1)
        repo.goto("4")
        self.assertEqual(self.files(), {"a.bin": b"v3"})

    def test_corruption_found_in_staging(self):
        # 事前検査(存在とヘッダ)を通っても、展開時の照合で見つかれば中止する。作業ファイルは未着手
        repo = self.build_linear(3)
        victim = self.chunk_of(repo, 2)
        helpers.flip_byte(victim)
        with self.assertRaises(BrokenVersion):
            repo.undo()
        self.assert_clean_at(repo, 3)
        self.assertEqual(self.files(), {"a.bin": b"v3"})
        self.assertEqual(self.quarantined(repo), [victim.name])
        # 隔離の記録があるので、次は事前検査で分かり、飛ばせる
        self.assertTrue(next(e.broken for e in repo.log() if e.id == 2))
        self.assertEqual(repo.undo(skip_broken=True).skipped, [2])
        self.assertEqual(self.files(), {"a.bin": b"v1"})


class TestVerify(CorruptionTestCase):
    # M4-9, M4-11: verify と修復、log の ✗(F-1, C-4, C-5, C-6, C-11、設計書 4.9節)。

    def test_f1_healthy(self):
        repo = self.build_linear(2)
        for quick in (False, True):
            r = repo.verify(quick=quick)
            self.assertTrue(r.ok)
            self.assertEqual(
                (
                    r.changed,
                    r.checked_chunks,
                    r.checked_manifests,
                    r.checked_commits,
                    r.broken_commits,
                ),
                (False, 3, 3, 3, []),
            )
        self.assertEqual(self.oplog(repo)[-1]["op"], "verify")

    def test_c4_broken_manifest_affects_only_its_versions(self):
        # 0: (a0, b0)、1: (a1, b0)、2: (a1, b2)。a1 のマニフェストを壊すと、壊れるのは 1 と 2 だけ
        self.write("a.bin", b"a0")
        self.write("b.bin", b"b0")
        repo = self.init(["*.bin"])
        self.write("a.bin", b"a1")
        repo.commit()
        self.write("b.bin", b"b2")
        repo.commit()
        sha = self.manifest_of(repo, 1)
        helpers.flip_byte(repo._store.manifest_path(sha))
        health = (repo.bvc_dir / "health.json").read_bytes()
        self.assertFalse(
            any(e.broken for e in repo.log())
        )  # 未検出のうちは ✗ を付けない
        self.assertEqual(
            (repo.bvc_dir / "health.json").read_bytes(), health
        )  # log は記録しない

        r = repo.verify()
        self.assertEqual(
            (r.ok, r.changed, r.broken_commits, r.bad_manifests),
            (False, True, [1, 2], [sha]),
        )
        self.assertEqual(self.quarantined(repo, "manifests"), [f"{sha}.json"])
        self.assertEqual(
            {e.id: e.broken for e in repo.log()}, {0: False, 1: True, 2: True}
        )
        repo.goto("0")
        self.assertEqual(self.files(), {"a.bin": b"a0", "b.bin": b"b0"})
        self.assertFalse(repo.verify().changed)  # 同じ異常は、2回目には変化なし

    def test_c11_fake_chunk_and_c5_quarantine(self):
        repo = self.build_linear(2)
        victim = self.chunk_of(repo, 1)
        victim.write_bytes(
            b"\x00" + b"xx"
        )  # 形式も長さも正しいが、名前と中身が合わない
        self.assertTrue(repo.verify(quick=True).ok)  # quick では見逃す
        r = repo.verify()
        self.assertEqual((r.broken_commits, r.bad_chunks), ([1], [victim.name]))
        self.assertEqual(self.quarantined(repo), [victim.name])
        self.assertTrue(next(e.broken for e in repo.log() if e.id == 1))
        # 隔離したものは重複排除で再利用せず、同じ内容を commit すると保存し直す
        self.write("a.bin", b"v1")
        repo.commit()
        self.assertEqual(victim.read_bytes()[1:], b"v1")
        self.assertTrue(repo.verify().ok)
        self.assertFalse(any(e.broken for e in repo.log()))

    def test_quick_detects_missing_chunk(self):
        repo = self.build_linear(2)
        self.chunk_of(repo, 1).unlink()
        r = repo.verify(quick=True)
        self.assertEqual((r.ok, r.broken_commits), (False, [1]))
        self.assertTrue(next(e.broken for e in repo.log() if e.id == 1))

    def test_c6_repair_from_tracked_file(self):
        repo = self.build_linear(2)
        repo.undo()
        victim = self.chunk_of(repo, 1)
        helpers.flip_byte(victim)
        self.assertEqual(repo.verify().broken_commits, [1])
        r = repo.verify(repair=True)
        self.assertEqual(
            (r.ok, r.changed, r.repaired_chunks, r.bad_chunks),
            (True, True, [victim.name], []),
        )
        self.assertFalse(any(e.broken for e in repo.log()))
        for cid in (0, 2, 1):
            repo.goto(str(cid))
            self.assertEqual(self.files(), {"a.bin": f"v{cid}".encode()})

    def test_c6_repair_from_untracked_files(self):
        # パターン外で、名前が同じファイル(別のフォルダ)とサイズが同じファイルを材料にする
        for how in ("name", "size"):
            with self.subTest(how=how):
                helpers.remove_tree(self.tmp)
                self.tmp.mkdir()
                repo = self.build_linear(2)
                self.set_config(repo, track=["*.bin"]).close()
                repo = self.reopen()
                self.chunk_of(repo, 1).unlink()
                self.assertFalse(repo.verify(repair=True).ok)  # 材料が無ければ直らない
                self.write("backup/a.bin" if how == "name" else "old.dat", b"v1")
                r = repo.verify(repair=True)
                self.assertTrue(r.ok, r)
                repo.goto("1")
                self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v1")
                repo.close()

    def test_repair_manifest(self):
        repo = self.build_linear(2)
        sha = self.manifest_of(repo, 2)
        repo._store.manifest_path(sha).unlink()
        r = repo.verify(repair=True)
        self.assertEqual(
            (r.ok, r.repaired_manifests, r.repaired_chunks), (True, [sha], [])
        )
        repo.goto("0")
        repo.goto("2")
        self.assertEqual(self.files(), {"a.bin": b"v2"})

    def test_unreadable_version_file(self):
        repo = self.build_linear(2)
        repo.close()
        helpers.break_json(self.tmp / ".bvc" / "commits" / "1.json")
        with self.assertLogs("bvc.history", "WARNING"):
            repo = self.reopen()
        r = repo.verify()
        self.assertEqual((r.ok, r.broken_commits), (False, [1]))
        self.assertEqual(
            repo._store.health.records("bad_commits")["1"]["reason"], "unreadable"
        )
        # 削除した版は検査の対象外(gc で消える)
        repo.discard("1")
        r = repo.verify()
        self.assertEqual((r.ok, r.checked_commits), (True, 2))

    def test_broken_data_of_discarded_version_is_not_reported(self):
        repo = self.build_linear(2)
        repo.discard("1")
        self.chunk_of(repo, 1).unlink()
        r = repo.verify()
        self.assertEqual((r.ok, r.broken_commits, r.bad_chunks), (True, [], []))


class TestRecoverControl(MoveTestCase):
    # M4-10: 管理ファイルの自動復旧(C-7、設計書 4.12節)。

    BREAKS = {  # noqa: RUF012
        "delete": lambda p: p.unlink(),
        "empty": lambda p: p.write_bytes(b""),
        "json": helpers.break_json,
        # JSON としては読めるが、必要な項目が無い・型が違う
        "invalid": lambda p: p.write_text(
            '{"format": 1, "bad_chunks": [], "names": 1, "entries": 1}', "utf-8"
        ),
    }

    def setUp(self):
        super().setUp()
        # 版 0 ← 1 ← 2(a.bin = v<番号>)。@ = 1、ブランチ 0 に名前 main
        repo = self.build_linear(2)
        repo.undo()
        repo.name_branch("main")
        repo.close()
        self.saved = self.tmp.parent / (self.tmp.name + "-saved")
        shutil.copytree(self.tmp / ".bvc", self.saved)
        self.addCleanup(helpers.remove_tree, self.saved)

    def restore_bvc(self):
        helpers.remove_tree(self.tmp / ".bvc")
        shutil.copytree(self.saved, self.tmp / ".bvc")

    def open_recovered(self, name):
        # 警告を出して復旧し、作業ファイルは変えず、oplog に記録する。2回目は何もしない
        with self.assertLogs("bvc.repo", "WARNING") as logs:
            repo = self.reopen()
        self.assertIn(name, "\n".join(logs.output))
        self.assertEqual(self.files(), {"a.bin": b"v1"})
        op = self.oplog(repo)[-1]
        self.assertEqual((op["op"], op["args"]["file"]), ("recover_control", name))
        repo.close()
        with self.assertNoLogs("bvc.repo", "WARNING"):
            repo = self.reopen()
        return repo

    def check_each_break(self, name, check):
        for how, damage in self.BREAKS.items():
            with self.subTest(file=name, damage=how):
                self.restore_bvc()
                damage(self.tmp / ".bvc" / name)
                repo = self.open_recovered(name)
                check(repo)
                repo.close()

    def test_c7_head(self):
        def check(repo):
            self.assertEqual(self.head(repo), Head(1, 0))  # 操作ログの最後の after
            self.write("a.bin", b"new")
            r = repo.commit()
            self.assertEqual((r.commit.id, r.new_branch), (3, True))
            self.write("a.bin", b"v1")

        self.check_each_break("HEAD.json", check)

    def test_c7_head_points_to_unknown_version(self):
        (self.tmp / ".bvc" / "HEAD.json").write_text(
            '{"format":1,"at":99,"branch":0}', "utf-8"
        )
        repo = self.open_recovered("HEAD.json")
        self.assertEqual(self.head(repo), Head(1, 0))

    def test_c7_head_without_oplog(self):
        # 操作ログが無ければ、作業ファイルと内容が一致する版。それも無ければ最新の版(作業ファイルは変えない)
        for edit, expected in ((None, Head(1, 0)), (b"edited", Head(2, 0))):
            with self.subTest(edit=edit):
                self.restore_bvc()
                (self.tmp / ".bvc" / "oplog.jsonl").unlink()
                (self.tmp / ".bvc" / "HEAD.json").unlink()
                if edit is not None:
                    self.write("a.bin", edit)
                with self.assertLogs("bvc.repo", "WARNING") as logs:
                    repo = self.reopen()
                self.assertEqual(self.head(repo), expected)
                if edit is not None:
                    self.assertIn("最新の版", "\n".join(logs.output))
                    self.assertEqual((self.tmp / "a.bin").read_bytes(), edit)
                    self.assertTrue(repo.work_state().dirty)
                    self.write("a.bin", b"v1")
                repo.close()

    def test_c7_counters(self):
        def check(repo):
            self.write("a.bin", b"new")
            self.assertEqual(repo.commit().commit.id, 3)
            self.write("a.bin", b"v1")

        self.check_each_break("counters.json", check)

    def test_c7_counters_do_not_reuse_numbers_removed_by_gc(self):
        repo = self.reopen()
        repo.goto("2")
        self.write("a.bin", b"v3")
        repo.commit()
        repo.undo()
        repo.discard("3")
        repo.gc()
        self.assertNotIn(3, commit_files(repo.bvc_dir))
        repo.close()
        (self.tmp / ".bvc" / "counters.json").unlink()
        with self.assertLogs("bvc.repo", "WARNING"):
            repo = self.reopen()
        self.write("a.bin", b"v4")
        self.assertEqual(repo.commit().commit.id, 4)

    def test_c7_index(self):
        def check(repo):
            self.assertFalse(repo.work_state().dirty)
            self.assertEqual(
                json.loads((self.tmp / ".bvc" / "index.json").read_text())["entries"],
                {},
            )

        self.check_each_break("index.json", check)

    def test_c7_branches(self):
        def check(repo):
            self.assertEqual([b.name for b in repo.branches()], [None])

        self.check_each_break("branches.json", check)

    def test_c7_health(self):
        def check(repo):
            self.assertEqual(repo._store.health.records("bad_chunks"), {})
            self.assertTrue(repo.verify().ok)

        self.check_each_break("health.json", check)

    def test_c7_config_is_not_recovered(self):
        for how, damage in self.BREAKS.items():
            with self.subTest(damage=how):
                self.restore_bvc()
                damage(self.tmp / ".bvc" / "config.json")
                before = helpers.tree_hashes(self.tmp / ".bvc")
                with self.assertRaises(BvcError) as cm:
                    Repo.open(self.tmp)
                self.assertIn("config.json", str(cm.exception))
                self.assertEqual(helpers.tree_hashes(self.tmp / ".bvc"), before)

    def test_c7_read_error_changes_nothing(self):
        # 読み込み自体の失敗(使用中など)は復旧せずに中止する(I-20)
        real = fsutil.read_bytes
        for name in (
            "HEAD.json",
            "counters.json",
            "branches.json",
            "index.json",
            "health.json",
            "config.json",
        ):
            with self.subTest(file=name):
                self.restore_bvc()
                before = helpers.tree_hashes(self.tmp / ".bvc")

                def busy(path, name=name):
                    if Path(path).name == name:
                        raise PermissionError(13, "使用中", str(path))
                    return real(path)

                with (
                    mock.patch.object(fsutil, "read_bytes", busy),
                    self.assertRaises(FileBusy),
                ):
                    Repo.open(self.tmp)
                self.assertEqual(helpers.tree_hashes(self.tmp / ".bvc"), before)
                self.assertFalse((self.tmp / ".bvc" / "lock").exists())

    def test_unknown_format_changes_nothing(self):
        for name in (
            "HEAD.json",
            "counters.json",
            "branches.json",
            "index.json",
            "health.json",
        ):
            with self.subTest(file=name):
                self.restore_bvc()
                (self.tmp / ".bvc" / name).write_text('{"format": 99}', "utf-8")
                before = helpers.tree_hashes(self.tmp / ".bvc")
                with self.assertRaises(UnsupportedFormat):
                    Repo.open(self.tmp)
                self.assertEqual(helpers.tree_hashes(self.tmp / ".bvc"), before)


if __name__ == "__main__":
    unittest.main()
