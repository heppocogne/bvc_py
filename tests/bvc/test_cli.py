# cli の単体テスト(M2-11, M2-12, M3-8, M4-1〜M4-11, M5-1〜M5-3)。観点: F-1, F-6, F-7, F-11, F-12, F-14, P-5, C-2, C-6。

import dataclasses
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bvc import cli
from bvc.model import (
    BranchInfo,
    CommitResult,
    DiscardResult,
    GcReport,
    MoveResult,
    Note,
    ProgressEvent,
    VerifyReport,
    WorkState,
)
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


class TestRename(CliTestCase):
    # M4-5, M4-6: --rename とヒントの表示(F-6、仕様書 2.6節・3.2節)

    def test_f6_rename_option(self):
        self.write("a.bin", b"aaa")
        self.bvc("init", "--track", "*.bin")
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", b"bbb")
        code, out, err = self.bvc("commit", "--rename", "a.bin=b.bin")
        self.assertEqual(code, 0, err)
        self.assertIn("renamed:  a.bin → b.bin", out)

    def test_f6_rename_option_errors(self):
        self.write("a.bin", b"aaa")
        self.bvc("init", "--track", "*.bin")
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", b"bbb")
        for spec in ("a.bin", "=b.bin", "a.bin=", "x.bin=b.bin"):
            with self.subTest(spec=spec):
                code, out, _ = self.bvc("--json", "commit", "--rename", spec)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["type"], "UsageError")

    def test_f6_similarity_shown_as_percent(self):
        Repo.init(self.tmp, track=["*.bin"], chunker={"name": "fixed", "size": 1024}).close()
        self.write("a.bin", b"x" * 3072 + b"a" * 1024)
        self.bvc("commit")
        (self.tmp / "a.bin").unlink()
        self.write("b.bin", b"x" * 3072 + b"b" * 1024)
        code, out, err = self.bvc("commit")
        self.assertEqual(code, 0, err)
        self.assertIn("renamed:  a.bin → b.bin (75%)", out)

    def test_f6_hint_on_abort(self):
        self.write("result.bin", b"content")
        self.bvc("init", "--track", "*.bin")
        (self.tmp / "result.bin").rename(self.tmp / "result.bin.tmp")
        code, _, err = self.bvc("commit")
        self.assertEqual(code, 3)
        self.assertIn("  missing: result.bin\n  ヒント: パターン外に同じ内容のファイルがあります: result.bin.tmp\n", err)
        code, out, _ = self.bvc("--json", "log")
        self.assertEqual(json.loads(out)["uncommitted"]["hints"], {"result.bin": ["result.bin.tmp"]})


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



class TestCorruptionCommands(CliTestCase):
    # M4-8, M4-9, M4-11(仕様書 2.9節・3.4節・3.10節)。観点: F-1, F-12, C-2, C-6。

    def setUp(self):
        super().setUp()
        self.write("a.bin", b"v0")
        self.bvc("init", "--track", "*.bin")
        for i in (1, 2):
            self.write("a.bin", f"v{i}".encode())
            self.bvc("commit", "-m", f"c{i}")

    def break_version(self, cid):
        # 版 cid の a.bin のチャンクを消す
        with Repo.open(self.tmp) as repo:
            m = repo._store.get_manifest(repo._history.get(cid).tree["a.bin"])
            repo._store.chunk_path(m.chunks[0].sha).unlink()

    def test_f1_verify(self):
        code, out, err = self.bvc("verify")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("検査しました: チャンク 3、マニフェスト 3、版 3", out)
        self.assertIn("異常はありません", out)
        code, out, _ = self.bvc("--json", "verify", "--quick")
        data = json.loads(out)
        self.assertEqual((code, data["ok"], data["changed"], data["quick"]), (0, True, False, True))

    def test_f12_verify_exit_code_and_repair(self):
        self.break_version(1)
        code, out, err = self.bvc("verify")
        self.assertEqual(code, 1)
        self.assertIn("壊れた版があります: 1", err)
        self.assertIn("--repair", err)
        code, out, _ = self.bvc("log")
        self.assertRegex(out, r"(?m)^✗  1 ")
        code, out, _ = self.bvc("--json", "verify")
        data = json.loads(out)
        self.assertEqual((code, data["ok"], data["broken_commits"]), (1, False, [1]))

        self.write("backup/a.bin", b"v1")
        code, out, err = self.bvc("verify", "--repair")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("修復しました: チャンク 1、マニフェスト 0", out)
        code, out, _ = self.bvc("log")
        self.assertNotIn("✗", out)

    def test_c2_skip_broken(self):
        self.break_version(1)
        code, _, err = self.bvc("undo")
        self.assertEqual(code, 1)
        self.assertIn("--skip-broken", err)
        self.assertEqual((self.tmp / "a.bin").read_bytes(), b"v2")
        code, out, _ = self.bvc("undo", "--skip-broken")
        self.assertEqual(code, 0)
        self.assertIn("版 0 に移動しました", out)
        self.assertIn("壊れた版 1 を飛ばしました", out)
        code, out, _ = self.bvc("--json", "redo", "--skip-broken")
        data = json.loads(out)
        self.assertEqual((code, data["after"]["at"], data["skipped"]), (0, 2, [1]))



# ---------------------------------------------------------------------------
# M5: --json・終了コード・進捗・警告・文字コード
# ---------------------------------------------------------------------------


def _keys(cls) -> set[str]:
    # --json の出力のキー: フィールド + 公開のプロパティ + warnings
    return {f.name for f in dataclasses.fields(cls)} | set(cli._public_properties(cls)) | {"warnings"}


class TestToJsonable(unittest.TestCase):
    def test_dataclass_properties_and_containers(self):
        s = WorkState(
            tree={"a": "0" * 64},
            modified=["a"],
            added=[],
            renamed=[("b", "c", 0.5)],
            missing=[],
            hints={},
            total_bytes=1,
            new_bytes=1,
            fs_time_ns=5,
        )
        d = cli.to_jsonable(s)
        self.assertIs(d["dirty"], True)
        self.assertEqual(d["renamed"], [["b", "c", 0.5]])
        self.assertEqual(json.loads(json.dumps(d)), d)
        self.assertEqual(
            cli.to_jsonable({1: Path("x/y"), "s": {"b", "a"}}),
            {"1": str(Path("x/y")), "s": ["a", "b"]},
        )
        v = VerifyReport(
            changed=False, quick=True, repair=False, checked_chunks=0, checked_manifests=0,
            checked_commits=0, bad_chunks=[], bad_manifests=[], broken_commits=[3],
            repaired_chunks=[], repaired_manifests=[],
        )
        self.assertIs(cli.to_jsonable(v)["ok"], False)


class TestJsonAllCommands(CliTestCase):
    # F-11: 全コマンドの --json が解析でき、dataclass の内容と一致する。changed と warnings を必ず含む。

    def json_cmd(self, *args, code=0):
        c, out, err = self.bvc("--json", *args)
        self.assertEqual(c, code, out)
        self.assertEqual(err, "")  # --json では stderr に何も出さない
        data = json.loads(out)
        self.assertIsInstance(data["changed"], bool)
        self.assertIsInstance(data["warnings"], list)
        return data

    def test_f11_all_commands(self):
        self.write("a.bin", b"v0")
        data = self.json_cmd("init", "--track", "*.bin")
        self.assertEqual(set(data), {"changed", "workdir", "commit", "warnings"})
        self.assertEqual(data["workdir"], str(self.tmp))
        with Repo.open(self.tmp) as repo:
            self.assertEqual(data["commit"], cli.to_jsonable(repo.get_commit(0)))

        self.write("a.bin", b"v1")
        data = self.json_cmd("commit", "-m", "one")
        self.assertEqual(set(data), _keys(CommitResult))
        with Repo.open(self.tmp) as repo:
            self.assertEqual(data["commit"], cli.to_jsonable(repo.get_commit(1)))
        self.assertEqual(data["state"]["modified"], ["a.bin"])
        self.assertIs(data["state"]["dirty"], True)

        data = self.json_cmd("note", "-m", "メモ")
        self.assertEqual(set(data["note"]), {f.name for f in dataclasses.fields(Note)})
        self.assertEqual(
            (data["changed"], data["note"]["commit_id"], data["note"]["text"]), (True, 1, "メモ")
        )

        data = self.json_cmd("undo")
        self.assertEqual(set(data), _keys(MoveResult))
        data = self.json_cmd("redo")
        self.assertEqual((data["changed"], data["after"]["at"]), (True, 1))
        data = self.json_cmd("goto", "0")
        self.assertEqual((set(data), data["restored"]), (_keys(MoveResult), ["a.bin"]))
        self.write("a.bin", b"v2")
        self.json_cmd("commit", "-m", "two")

        data = self.json_cmd("branch", "name", "本線", "1")
        self.assertEqual(set(data["branch"]), {f.name for f in dataclasses.fields(BranchInfo)})
        data = self.json_cmd("branch")
        with Repo.open(self.tmp) as repo:
            self.assertEqual(data["branches"], cli.to_jsonable(repo.branches()))
        data = self.json_cmd("branch", "unname", "本線")
        self.assertIsNone(data["branch"]["name"])

        self.write("a.bin", b"dirty")
        data = self.json_cmd("log", "--discarded")
        self.assertEqual(set(data), {"changed", "uncommitted", "entries", "warnings"})
        with Repo.open(self.tmp) as repo:
            self.assertEqual(data["entries"], cli.to_jsonable(repo.log(include_discarded=True)))
            expected = cli.to_jsonable(repo.work_state())
        # 走査の時刻は呼ぶたびに変わる
        for d in (data["uncommitted"], expected):
            d.pop("fs_time_ns")
        self.assertEqual(data["uncommitted"], expected)
        self.json_cmd("commit")

        data = self.json_cmd("discard", "1")
        self.assertEqual((set(data), data["discarded"]), (_keys(DiscardResult), 1))
        data = self.json_cmd("gc", "--dry-run")
        self.assertEqual(
            (set(data), data["changed"], data["deleted_commits"]), (_keys(GcReport), False, [1])
        )
        data = self.json_cmd("gc")
        self.assertEqual((data["changed"], data["deleted_commits"]), (True, [1]))
        data = self.json_cmd("verify")
        self.assertEqual((set(data), data["ok"]), (_keys(VerifyReport), True))

        data = self.json_cmd("--version")
        self.assertEqual(set(data), {"changed", "version", "warnings"})

    def test_m5_3_warnings_in_json(self):
        # 警告は stderr ではなく warnings 配列に入る
        data = self.json_cmd("init", "--track", "*.bin")
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("追跡対象のファイルがありません", data["warnings"][0])

    def test_m5_3_warnings_with_result(self):
        self.write("a.bin")
        self.bvc("init", "--track", "*.bin")
        (self.tmp / ".bvc" / "branches.json").write_text("{", "utf-8")
        self.write("a.bin", b"y")
        data = self.json_cmd("commit")
        self.assertIs(data["changed"], True)
        self.assertTrue(any("branches.json" in w for w in data["warnings"]), data["warnings"])

    def test_f11_error_json(self):
        data = self.json_cmd("log", code=1)
        self.assertEqual(set(data), {"changed", "error", "type", "exit_code", "details", "warnings"})
        self.assertEqual((data["changed"], data["exit_code"]), (False, 1))
        self.assertIn("リポジトリが見つかりません", data["error"])


class TestExitCodesAllCommands(CliTestCase):
    # F-12: 全コマンドの終了コード(仕様書 2.2節)。4 は TestMove・TestHistoryCommands などで確認している。

    VALID = [
        ("commit",),
        ("log",),
        ("undo",),
        ("redo",),
        ("goto", "0"),
        ("note", "-m", "x"),
        ("branch",),
        ("branch", "name", "x"),
        ("branch", "unname", "x"),
        ("discard",),
        ("gc",),
        ("verify",),
    ]
    INVALID = [
        (),
        ("init",),
        ("commit", "--bogus"),
        ("log", "-n", "x"),
        ("undo", "--bogus"),
        ("redo", "--bogus"),
        ("goto",),
        ("note",),
        ("branch", "name"),
        ("branch", "bogus"),
        ("discard", "1", "2"),
        ("gc", "--bogus"),
        ("verify", "--bogus"),
        ("nosuchcommand",),
    ]

    def test_f12_usage_error_is_2(self):
        for args in self.INVALID:
            with self.subTest(args=args):
                code, out, err = self.bvc(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage:", err)
                self.assertIn("エラー: ", err)
                code, out, err = self.bvc("--json", *args)
                self.assertEqual((code, err), (2, ""))
                data = json.loads(out)
                self.assertEqual(
                    (data["type"], data["exit_code"], data["changed"]), ("UsageError", 2, False)
                )

    def test_f12_no_repo_is_1(self):
        for args in self.VALID:
            with self.subTest(args=args):
                code, out, _ = self.bvc("--json", *args)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["exit_code"], 1)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_f12_locked_is_3(self):
        self.write("a.bin")
        self.bvc("init", "--track", "*.bin")
        lock = self.tmp / ".bvc" / "lock"
        lock.write_text("{}", "utf-8")
        for args in self.VALID:
            with self.subTest(args=args):
                code, out, _ = self.bvc("--json", *args)
                self.assertEqual((code, json.loads(out)["type"]), (3, "Locked"))
                code, _, err = self.bvc(*args)
                self.assertEqual(code, 3)
                self.assertIn("中止: 別の bvc", err)
        code, _, _ = self.bvc("init", "--track", "*")
        self.assertEqual(code, 1)
        lock.unlink()
        self.assertEqual(self.bvc("log")[0], 0)


class _Tty(io.StringIO):
    def isatty(self):
        return True


class TestProgress(CliTestCase):
    # M5-2: 進捗は標準エラーが端末のときだけ表示する。

    def run_tty(self, *args):
        out, err = io.StringIO(), _Tty()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(["-C", str(self.tmp), *args])
        return code, out.getvalue(), err.getvalue()

    def test_progress_on_tty(self):
        self.write("a.bin", b"x" * 100_000)
        code, out, err = self.run_tty("init", "--track", "*.bin")
        self.assertEqual(code, 0)
        self.assertIn("\r保存 a.bin: ", err)
        self.assertIn("(100%)", err)
        # 最後に進捗の行を消す
        self.assertRegex(err, r"\r +\r$")
        self.assertIn("リポジトリを作成しました", out)
        self.assertNotIn("\r", out)

    def test_progress_moves_and_verify(self):
        self.write("a.bin", b"1" * 1000)
        self.bvc("init", "--track", "*.bin")
        self.write("a.bin", b"2" * 1000)
        self.bvc("commit")
        _, _, err = self.run_tty("undo")
        self.assertIn("置き換え a.bin: 1/1 (100%)", err)
        _, _, err = self.run_tty("verify")
        self.assertIn("チャンクの検査", err)

    def test_no_progress_when_quiet_or_json_or_not_tty(self):
        self.write("a.bin", b"x" * 1000)
        code, _, err = self.run_tty("-q", "init", "--track", "*.bin")
        self.assertEqual((code, err), (0, ""))
        self.write("a.bin", b"y" * 1000)
        code, out, err = self.run_tty("--json", "commit")
        self.assertEqual((code, err), (0, ""))
        json.loads(out)
        self.write("a.bin", b"z" * 1000)
        code, _, err = self.bvc("commit")
        self.assertEqual((code, err), (0, ""))

    def test_message_clears_progress_line(self):
        err = _Tty()
        view = cli.ProgressView(err, interval=0)
        view(ProgressEvent("put", 10, 100, "a.bin"))
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            cli.setup_logging(progress=view)
            try:
                cli.logger.warning("注意")
            finally:
                cli.setup_logging()
        line = "保存 a.bin: 10 B / 100 B (10%)"
        self.assertEqual(err.getvalue(), f"\r{line}\r{' ' * cli._width(line)}\r警告: 注意\n")


class TestProgressView(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            cli.format_progress(ProgressEvent("put", 1024, 2048, "a")), "保存 a: 1.0 KiB / 2.0 KiB (50%)"
        )
        self.assertEqual(cli.format_progress(ProgressEvent("gc", 3, 4)), "削除: 3/4 (75%)")
        self.assertEqual(cli.format_progress(ProgressEvent("new_stage", 3)), "new_stage: 3")
        self.assertEqual(cli.format_progress(ProgressEvent("restore", 0, 0)), "置き換え: 0/0")

    def test_throttle_and_clear(self):
        s = _Tty()
        view = cli.ProgressView(s, interval=3600)
        view(ProgressEvent("put", 1, 10, "a"))
        view(ProgressEvent("put", 2, 10, "a"))  # 間隔内の同じファイルは表示しない
        view(ProgressEvent("put", 1, 10, "b"))  # ファイルが変わったら表示する
        self.assertEqual(s.getvalue().count("\r"), 2)
        self.assertNotIn("2 B", s.getvalue())
        view.clear()
        view.clear()  # 2回目は何もしない
        self.assertEqual(s.getvalue().count("\r"), 4)

    def test_long_line_is_truncated(self):
        line = cli._truncate("保存 " + "長" * 100 + ": 1 B", 20)
        self.assertLessEqual(cli._width(line), 20)
        self.assertTrue(line.startswith("…") and line.endswith(": 1 B"))


class TestEncoding(CliTestCase):
    # M4-C の残課題: stdout が cp932 のパイプでも表示できる。表せない文字は置き換え、JSON は ASCII で書く。

    def run_cp932(self, *args):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp932", errors="strict", newline="\n")
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(["-C", str(self.tmp), *args])
        out.flush()
        return code, raw.getvalue().decode("cp932"), err.getvalue()

    def test_log_on_cp932(self):
        self.write("a.bin", b"1")
        self.bvc("init", "--track", "*.bin")
        self.write("a.bin", b"2")
        self.bvc("commit")
        self.bvc("undo")
        self.write("a.bin", b"3")
        self.bvc("commit")
        name = "é\U0001f600.bin"  # cp932 で表せない名前
        self.write(name, b"4")
        code, out, err = self.run_cp932("log")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("├─┘", out)
        self.assertIn("??.bin (added)", out)
        code, out, err = self.run_cp932("--json", "log")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.isascii())
        self.assertEqual(json.loads(out)["uncommitted"]["added"], [name])

    def test_symbol_fallback(self):
        class Ascii:
            encoding = "ascii"

        class Sjis:
            encoding = "cp932"

        self.assertEqual(cli._fallback("✗ ╯ ├─┼│ ○ →", Ascii()), "x / |-+| o ->")
        self.assertEqual(cli._fallback("✗╯├", Sjis()), "×┘├")
        self.assertEqual(cli._fallback("✗", io.StringIO()), "✗")

if __name__ == "__main__":
    unittest.main()
