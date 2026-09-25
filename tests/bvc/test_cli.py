# cli の単体テスト(M2-11, M2-12)。観点: F-1, F-7, F-12, P-5。

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from bvc import cli
from bvc.model import Head
from bvc.repo import Repo
from tests import helpers


class CliTestCase(helpers.TempDirTestCase):
    def bvc(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(["-C", str(self.tmp), *args])
        return code, out.getvalue(), err.getvalue()

    def write(self, rel, data=b"x"):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class TestCommands(CliTestCase):
    def test_f1_init_commit_log(self):
        self.write("a.bin", b"1")
        code, out, err = self.bvc("init", "--track", "*.bin")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("追跡ファイル 1 件", out)

        self.write("a.bin", b"2")
        self.write("b.bin", b"3")
        code, out, _ = self.bvc("commit", "-m", "変更")
        self.assertEqual(code, 0)
        self.assertIn("版 1 を作成しました", out)
        self.assertIn("modified: a.bin", out)
        self.assertIn("added:    b.bin", out)

        code, out, _ = self.bvc("log")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^@  1  \d\d-\d\d \d\d:\d\d  変更$")
        self.assertRegex(lines[1], r"^○  0  .*\(init\)$")

    def test_f12_no_change(self):
        self.bvc("init", "--track", "*")
        code, out, _ = self.bvc("commit")
        self.assertEqual(code, 0)
        self.assertIn("変更なし", out)
        code, out, _ = self.bvc("--json", "commit")
        self.assertEqual(code, 0)
        self.assertIs(json.loads(out)["changed"], False)

    def test_f7_missing_exit_3_and_lock_released(self):
        self.write("a", b"1")
        self.bvc("init", "--track", "*")
        (self.tmp / "a").unlink()
        code, out, err = self.bvc("commit")
        self.assertEqual(code, 3)
        self.assertIn("中止: 追跡ファイルが見つかりません", err)
        self.assertIn("missing: a", err)
        code, _, _ = self.bvc("log")
        self.assertEqual(code, 0)
        code, out, _ = self.bvc("commit", "--allow-missing")
        self.assertEqual(code, 0)
        self.assertIn("deleted:  a", out)

    def test_p5_subdir_and_default_cwd_search(self):
        self.write("a", b"1")
        self.write("sub/b", b"2")
        self.bvc("init", "--track", "**")
        self.write("sub/b", b"3")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(["-C", str(self.tmp / "sub"), "commit"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("modified: sub/b", out.getvalue())

    def test_init_requires_track(self):
        code, _, _ = self.bvc("init")
        self.assertEqual(code, 2)
        self.assertFalse((self.tmp / ".bvc").exists())

    def test_init_twice_is_error(self):
        self.bvc("init", "--track", "*")
        code, _, err = self.bvc("init", "--track", "*")
        self.assertEqual(code, 1)
        self.assertIn("エラー:", err)

    def test_open_without_repo(self):
        code, _, err = self.bvc("log")
        self.assertEqual(code, 1)
        self.assertIn("リポジトリが見つかりません", err)

    def test_quiet_and_warning(self):
        code, out, err = self.bvc("-q", "init", "--track", "*.bin")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("警告: 追跡対象のファイルがありません", err)

    def test_json_error(self):
        self.write("a", b"1")
        self.bvc("init", "--track", "*")
        (self.tmp / "a").unlink()
        code, out, _ = self.bvc("--json", "commit")
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertEqual((data["type"], data["details"]["missing"]), ("MissingFiles", ["a"]))


class TestLogTree(CliTestCase):
    def test_m2_done_branch_tree(self):
        # 完了条件: 分岐を含む履歴を作成し、log でツリーとして表示できる
        self.write("a", b"0")
        self.bvc("init", "--track", "*")
        self.write("a", b"1")
        self.bvc("commit", "-m", "one")
        self.write("a", b"2")
        self.bvc("commit", "-m", "two")
        with Repo.open(self.tmp) as repo:
            repo._history.set_head(Head(1, 0))  # undo の代わり(M3)
        self.write("a", b"3")
        code, out, _ = self.bvc("commit", "-m", "three")
        self.assertIn("新しいブランチを作成", out)
        self.write("a", b"4")

        code, out, _ = self.bvc("log")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "未コミットの変更: a (modified)")
        self.assertEqual([ln.split("  ")[0] for ln in lines[1:]], ["@", "│ ○", "├─╯", "○", "○"])
        self.assertTrue(lines[1].endswith("three"))
        self.assertTrue(lines[2].endswith("two"))

    def test_render_multiple_branches(self):
        # 3 本の枝が同じ親に合流する
        e = cli.LogEntry
        entries = [
            e(id=5, commit=None, effective_parent=1),
            e(id=4, commit=None, effective_parent=1),
            e(id=3, commit=None, effective_parent=2),
            e(id=2, commit=None, effective_parent=1),
            e(id=1, commit=None, effective_parent=None, is_current=True),
        ]
        lines = cli._graph_lines(entries)
        graph = [ln.split("  ")[0] for ln in lines]
        self.assertEqual(graph, ["○", "│ ○", "├─╯", "│ ○", "│ ○", "├─╯", "@"])


if __name__ == "__main__":
    unittest.main()
