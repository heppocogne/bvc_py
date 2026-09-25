# M2 の基本テスト(M2-10, M2-11)。

import tempfile
import unittest
from pathlib import Path

from bvc.repo import Repo


class TestRepoBasic(unittest.TestCase):
    # Repo の基本操作をテスト。

    def setUp(self):
        # 一時フォルダを作る
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_init(self):
        # init でリポジトリを作成できる(M2-10)。

        repo = Repo.init(
            workdir=self.workdir,
            track=["*"],
            ignore=[],
        )
        repo.close()

        # .bvc フォルダが作られている
        self.assertTrue((self.workdir / ".bvc").is_dir())

        # 管理ファイルがある
        self.assertTrue((self.workdir / ".bvc" / "config.json").exists())
        self.assertTrue((self.workdir / ".bvc" / "HEAD.json").exists())
        self.assertTrue((self.workdir / ".bvc" / "counters.json").exists())

    def test_open_existing(self):
        # 既存のリポジトリを開ける(M2-10)。

        # init で作成
        repo1 = Repo.init(
            workdir=self.workdir,
            track=["*"],
            ignore=[],
        )
        repo1.close()

        # open で開く
        repo2 = Repo.open(self.workdir)
        self.assertIsNotNone(repo2)
        repo2.close()

    def test_find_repodir_from_subdir(self):
        # サブフォルダから .bvc を探索できる(M2-10)。

        # init で作成
        repo = Repo.init(
            workdir=self.workdir,
            track=["*"],
            ignore=[],
        )
        repo.close()

        # サブフォルダを作る
        subdir = self.workdir / "subdir"
        subdir.mkdir()

        # サブフォルダから探す
        found = Repo.find_repodir(subdir)
        self.assertEqual(found, self.workdir)

    def test_commit_no_changes(self):
        # 変更がないときは changed=False を返す(M2-10)。

        repo = Repo.init(
            workdir=self.workdir,
            track=["*"],
            ignore=[],
        )

        result = repo.commit(message="test")

        self.assertFalse(result.changed)
        self.assertIsNone(result.commit)

        repo.close()

    def test_commit_with_file(self):
        # ファイルを作ってコミットできる(M2-10)。

        repo = Repo.init(
            workdir=self.workdir,
            track=["*.txt"],
            ignore=[],
        )

        # ファイルを作る
        (self.workdir / "test.txt").write_text("hello")

        # 作業フォルダの状態を確認
        state = repo.work_state()
        print(f"state.tree: {state.tree}")
        print(f"state.added: {state.added}")
        print(f"state.modified: {state.modified}")
        print(f"state.dirty: {state.dirty}")

        # コミット
        result = repo.commit(message="add test.txt")

        print(f"result.changed: {result.changed}")
        print(f"result.state.tree: {result.state.tree}")
        print(f"result.state.dirty: {result.state.dirty}")

        self.assertTrue(result.changed, f"changed is {result.changed}, state.dirty={result.state.dirty}")
        self.assertIsNotNone(result.commit)
        self.assertEqual(result.commit.message, "add test.txt")

        repo.close()

    def test_log_empty(self):
        # 空のリポジトリの log は版 0 だけを返す(M2-10)。

        repo = Repo.init(
            workdir=self.workdir,
            track=["*"],
            ignore=[],
        )

        entries = repo.log()

        # 版 0(init) が1件
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].commit.kind, "init")

        repo.close()


    def test_multiple_commits(self):
        # 連続した複数のコミットができる(M2-1)。

        repo = Repo.init(
            workdir=self.workdir,
            track=["*.txt"],
            ignore=[],
        )

        # 版 1: ファイル a を追加
        (self.workdir / "a.txt").write_text("a")
        result1 = repo.commit(message="add a.txt")
        self.assertTrue(result1.changed)
        commit1_id = result1.commit.id
        self.assertFalse(result1.new_branch)  # 最初のコミットなので新しいブランチではない

        # 版 2: ファイル b を追加
        (self.workdir / "b.txt").write_text("b")
        result2 = repo.commit(message="add b.txt")
        self.assertTrue(result2.changed)
        commit2_id = result2.commit.id
        self.assertFalse(result2.new_branch)  # @ に子がないので新しいブランチではない

        # 版 3: ファイル c を追加
        (self.workdir / "c.txt").write_text("c")
        result3 = repo.commit(message="add c.txt")
        self.assertTrue(result3.changed)
        commit3_id = result3.commit.id
        self.assertFalse(result3.new_branch)

        # log で全版を確認
        entries = repo.log()
        # 版 3, 2, 1, 0(init)
        commit_ids = [e.commit.id for e in entries if e.commit]
        self.assertEqual(len(commit_ids), 4)
        self.assertEqual(commit_ids[0], commit3_id)  # 最新のコミットが先頭
        self.assertEqual(commit_ids[1], commit2_id)
        self.assertEqual(commit_ids[2], commit1_id)

        repo.close()


if __name__ == "__main__":
    unittest.main()
