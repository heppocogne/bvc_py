# cli の単体テスト(M2-11, M2-12, M3-8, M4-1〜M4-4)。観点: F-1, F-7, F-12, F-14, P-5。

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from bvc import cli
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


class TestMoveCommands(CliTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.bin", b"0")
        self.bvc("init", "--track", "*.bin")
        self.write("a.bin", b"1")
        self.bvc("commit", "-m", "one")

    def test_f1_undo_redo_goto(self):
        self.write("b.bin", b"new")
        code, out, err = self.bvc("undo", "-m", "やり直し")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("版 0 に移動しました", out)
        self.assertIn("版 2 に自動コミットしました(auto: before undo)", out)
        self.assertIn("restored: a.bin", out)
        self.assertIn("deleted:  b.bin", out)
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"0")
        code, out, _ = self.bvc("redo")
        self.assertEqual(code, 0)
        self.assertIn("版 1 に移動しました", out)
        code, out, _ = self.bvc("goto", "2")
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "b.bin").read_bytes(), b"new")
        with Repo.open(self.tmp) as repo:
            ops = [o for o in repo.log()]
        self.assertEqual(ops[0].commit.kind, "auto")

    def test_f12_exit_codes(self):
        code, _, err = self.bvc("redo")
        self.assertEqual(code, 4)
        self.assertIn("先端", err)
        code, out, _ = self.bvc("goto", "1")
        self.assertEqual(code, 0)
        self.assertIn("変更なし", out)
        code, out, _ = self.bvc("--json", "goto", "@")
        self.assertEqual(code, 0)
        self.assertIs(json.loads(out)["changed"], False)
        self.bvc("undo")
        code, _, _ = self.bvc("undo")
        self.assertEqual(code, 4)
        code, out, _ = self.bvc("--json", "undo")
        self.assertEqual((code, json.loads(out)["type"]), (4, "CannotMove"))
        code, _, _ = self.bvc("goto", "99")
        self.assertEqual(code, 1)
        code, _, _ = self.bvc("goto")
        self.assertEqual(code, 2)

    def test_json_move_result(self):
        code, out, _ = self.bvc("--json", "undo")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIs(data["changed"], True)
        self.assertEqual((data["before"], data["after"]), ({"at": 1, "branch": 0}, {"at": 0, "branch": 0}))
        self.assertEqual(data["restored"], ["a.bin"])

    def test_f7_missing_on_move(self):
        (self.tmp / "a.bin").unlink()
        code, _, err = self.bvc("undo")
        self.assertEqual(code, 3)
        self.assertIn("missing: a.bin", err)
        code, _, _ = self.bvc("undo", "--allow-missing")
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"0")


class TestLogTree(CliTestCase):
    def test_m2_done_branch_tree(self):
        # 完了条件: 分岐を含む履歴を作成し、log でツリーとして表示できる
        self.write("a", b"0")
        self.bvc("init", "--track", "*")
        self.write("a", b"1")
        self.bvc("commit", "-m", "one")
        self.write("a", b"2")
        self.bvc("commit", "-m", "two")
        self.bvc("undo")
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


class TestHistoryCommands(CliTestCase):
    # M4-1〜M4-4(仕様書 3.6〜3.9節)。観点: F-1, F-12, F-14。

    def setUp(self):
        super().setUp()
        self.write("a.bin", b"v0")
        self.bvc("init", "--track", "*.bin")
        for i in (1, 2):
            self.write("a.bin", f"v{i}".encode())
            self.bvc("commit", "-m", f"c{i}")

    def notes(self):
        code, out, _ = self.bvc("--json", "log", "--discarded")
        return {e["id"]: [n["text"] for n in e["notes"]] for e in json.loads(out)["entries"]}

    def test_f14_note_forms(self):
        code, out, _ = self.bvc("note", "-m", "2")
        self.assertEqual(code, 0)
        self.assertIn("版 2(c2)にコメントを追加しました", out)
        self.assertEqual(self.bvc("note", "-m", "2 回目")[0], 0)
        self.assertEqual(self.bvc("note", "-m", "x", "-r", "0")[0], 0)
        self.assertEqual(self.notes(), {2: ["2", "2 回目"], 1: [], 0: ["x"]})
        code, out, _ = self.bvc("log")
        self.assertIn("note: 2 回目", out)
        for args in (("note",), ("note", "-m", ""), ("note", "2")):
            with self.subTest(args=args):
                self.assertEqual(self.bvc(*args)[0], 2)
        self.assertEqual(self.bvc("note", "-m", "x", "-r", "9")[0], 1)
        self.assertEqual(self.notes(), {2: ["2", "2 回目"], 1: [], 0: ["x"]})
        code, out, _ = self.bvc("--json", "note", "-m", "j")
        self.assertEqual(json.loads(out)["note"]["text"], "j")

    def test_f1_branch(self):
        code, out, _ = self.bvc("branch")
        self.assertEqual((code, out), (0, "* (名前なし)  先端 2  分岐元 なし\n"))
        self.bvc("undo")
        self.write("a.bin", b"v3")
        self.bvc("commit", "-m", "c3")
        self.assertEqual(self.bvc("branch", "name", "本線", "2")[0], 0)
        code, out, _ = self.bvc("branch")
        self.assertEqual(out, "  本線        先端 2  分岐元 なし\n* (名前なし)  先端 3  分岐元 1\n")
        code, out, _ = self.bvc("--json", "branch")
        self.assertEqual([b["name"] for b in json.loads(out)["branches"]], ["本線", None])
        self.assertEqual(self.bvc("goto", "本線")[0], 0)
        code, out, _ = self.bvc("branch", "unname", "本線")
        self.assertIn("名前 '本線' を外しました", out)
        self.assertEqual(self.bvc("branch", "unname", "本線")[0], 1)
        self.assertEqual(self.bvc("branch", "name", "12")[0], 2)

    def test_f1_discard_and_gc(self):
        code, out, _ = self.bvc("discard", "1")
        self.assertEqual(code, 0)
        self.assertEqual(out, "版 1 に削除の印を付けました\n")
        code, out, _ = self.bvc("discard")
        self.assertEqual(code, 0)
        self.assertIn("版 0 に移動しました", out)
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v0")
        code, out, _ = self.bvc("gc", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("削除対象: 版 1, 2、マニフェスト 2、チャンク 2", out)
        code, out, _ = self.bvc("gc")
        self.assertIn("削除しました: 版 1, 2", out)
        code, out, _ = self.bvc("--json", "gc")
        self.assertEqual((code, json.loads(out)["changed"]), (0, False))
        code, out, _ = self.bvc("gc")
        self.assertIn("削除対象がありません", out)

    def test_f12_discard_exit_codes(self):
        self.bvc("goto", "0")
        code, _, err = self.bvc("discard")  # 根の現在位置
        self.assertEqual(code, 4)
        self.assertIn("根", err)
        self.assertEqual(self.bvc("discard", "9")[0], 1)
        code, out, _ = self.bvc("--json", "discard", "2")
        self.assertEqual((code, json.loads(out)["discarded"]), (0, 2))


if __name__ == "__main__":
    unittest.main()
