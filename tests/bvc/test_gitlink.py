# gitlink の単体テスト(M6-1, M6-2, M6-6)。観点: G-1(形式), G-6, P-1(bvc.lock), V-2。
# git を呼ぶテストは、git が無ければ skip する。

import json
import shutil
import subprocess
import unicodedata
import unittest
from pathlib import Path
from typing import Final

from bvc import gitlink
from bvc.errors import CorruptData, GitFailed, UnsafePath, UnsupportedFormat
from bvc.model import ChunkRef, LockFile, Manifest
from tests import helpers

SHA_A: Final[str] = "a" * 64
SHA_B: Final[str] = "b" * 64


def manifest(size: int, sha: str) -> Manifest:
    return Manifest(
        size=size, sha256=sha, chunker={"name": "whole"}, chunks=(ChunkRef(sha, size),)
    )


def lock_obj(files: dict, **kw) -> bytes:
    return json.dumps({"format": 1, "bvc_commit": 3, "files": files, **kw}).encode(
        "utf-8"
    )


def entry(sha: str = SHA_A, size: int = 1) -> dict:
    return {"size": size, "sha256": sha, "manifest": sha}


class TestLockFormat(unittest.TestCase):
    def test_g1_lock_bytes_is_sorted_and_stable(self):
        tree = {"b/y.bin": SHA_B, "a.bin": SHA_A}
        ms = {SHA_A: manifest(1, SHA_B), SHA_B: manifest(2, SHA_A)}
        data = gitlink.lock_bytes(5, tree, ms)
        self.assertTrue(data.endswith(b"}\n"))
        self.assertEqual(
            data, gitlink.lock_bytes(5, dict(reversed(list(tree.items()))), ms)
        )
        text = data.decode("utf-8")
        self.assertLess(text.index('"a.bin"'), text.index('"b/y.bin"'))
        self.assertLess(text.index('"bvc_commit"'), text.index('"files"'))
        # 1ファイルの変更が、そのファイルの行だけの差分になる(1行1項目)
        self.assertIn('\n      "size": 1\n', text)
        lock = gitlink.parse_lock(data)
        self.assertEqual(lock.bvc_commit, 5)
        self.assertEqual(lock.tree, tree)
        self.assertEqual(lock.files["a.bin"].size, 1)
        self.assertEqual(lock.files["a.bin"].sha256, SHA_B)

    def test_tree_hash(self):
        self.assertEqual(
            gitlink.tree_hash({"a": SHA_A, "b": SHA_B}),
            gitlink.tree_hash({"b": SHA_B, "a": SHA_A}),
        )
        self.assertNotEqual(
            gitlink.tree_hash({"a": SHA_A}), gitlink.tree_hash({"a": SHA_B})
        )
        self.assertRegex(gitlink.tree_hash({}), "^[0-9a-f]{64}$")

    def test_parse_accepts_missing_commit_and_bom(self):
        lock = gitlink.parse_lock(
            b"\xef\xbb\xbf" + json.dumps({"format": 1, "files": {}}).encode()
        )
        self.assertEqual(lock, LockFile(bvc_commit=None, files={}))

    def test_v2_unknown_format(self):
        with self.assertRaises(UnsupportedFormat):
            gitlink.parse_lock(json.dumps({"format": 2, "files": {}}).encode())

    def test_corrupt_values(self):
        for data in (
            b"",
            b"{",
            b"[]",
            json.dumps({"files": {}}).encode(),
            json.dumps({"format": 1}).encode(),
            lock_obj({}, bvc_commit=-1),
            lock_obj({}, bvc_commit="3"),
            lock_obj({"a": entry(size=-1)}),
            lock_obj({"a": entry(size=True)}),
            lock_obj({"a": {**entry(), "sha256": "x"}}),
            lock_obj({"a": {**entry(), "manifest": None}}),
            lock_obj({"a": "x"}),
            b"\xff\xfe",
        ):
            with self.subTest(data=data), self.assertRaises(CorruptData):
                gitlink.parse_lock(data)

    def test_p1_unsafe_paths(self):
        # P-1: 作業フォルダの外・.bvc の中・予約名・正規化されていない名前などは UnsafePath
        nfd = unicodedata.normalize("NFD", "が.bin")
        for path in (
            "../x",
            "/x",
            "C:/x",
            "C:\\x",
            "\\\\server\\x",
            ".bvc/config.json",
            "CON",
            "a/./b",
            "a//b",
            "a\x01",
            nfd,
            "x. ",
        ):
            with self.subTest(path=path), self.assertRaises(UnsafePath):
                gitlink.parse_lock(lock_obj({path: entry()}))


class TestLockFiles(helpers.TempDirTestCase):
    def test_read_write_lock(self):
        self.assertIsNone(gitlink.read_lock(self.tmp / "bvc.lock"))
        (self.tmp / ".bvc" / "tmp").mkdir(parents=True)
        data = gitlink.lock_bytes(1, {"a": SHA_A}, {SHA_A: manifest(1, SHA_A)})
        gitlink.write_lock(self.tmp, "sub/bvc.lock", data, self.tmp / ".bvc" / "tmp")
        self.assertEqual((self.tmp / "sub" / "bvc.lock").read_bytes(), data)
        self.assertEqual(
            gitlink.read_lock(self.tmp / "sub" / "bvc.lock").tree, {"a": SHA_A}
        )

    def test_p1_write_lock_checks_path(self):
        (self.tmp / ".bvc" / "tmp").mkdir(parents=True)
        for rel in ("../x.lock", ".bvc/x.lock"):
            with self.subTest(rel=rel), self.assertRaises(UnsafePath):
                gitlink.write_lock(self.tmp, rel, b"{}", self.tmp / ".bvc" / "tmp")
        self.assertFalse((self.tmp.parent / "x.lock").exists())

    def test_lockstate(self):
        bvc = self.tmp / ".bvc"
        (bvc / "tmp").mkdir(parents=True)
        self.assertEqual(gitlink.read_lockstate(bvc), (False, None))
        gitlink.write_lockstate(bvc, SHA_A)
        self.assertEqual(gitlink.read_lockstate(bvc), (True, SHA_A))
        gitlink.write_lockstate(bvc, None)
        self.assertEqual(gitlink.read_lockstate(bvc), (True, None))
        # 壊れていたら「記録あり・内容不明」(bvc.lock を作らない・黙って書き直さない側)
        (bvc / "lockstate.json").write_text('{"format":1,"tree_hash":"x"}')
        with self.assertLogs("bvc.gitlink", "WARNING"):
            self.assertEqual(gitlink.read_lockstate(bvc), (True, None))
        (bvc / "lockstate.json").write_text('{"format":9}')
        with self.assertRaises(UnsupportedFormat):
            gitlink.read_lockstate(bvc)


class TestHooks(helpers.TempDirTestCase):
    def test_hook_script(self):
        # 作業フォルダが git の作業ツリーの最上位なら -C は付けない(N-50)
        text = gitlink.hook_script("pre-commit", "", "python -m bvc")
        self.assertTrue(text.startswith("#!/bin/sh\n" + gitlink.HOOK_MARK + "\n"))
        self.assertIn("exec python -m bvc git pre-commit\n", text)
        # 最上位でなければ、最上位からの相対パスを付ける
        wd = 'sub dir/$x`y"z'
        text = gitlink.hook_script("pre-commit", wd, "python -m bvc")
        self.assertIn(
            'exec python -m bvc -C "sub dir/\\$x\\`y\\"z" git pre-commit\n', text
        )
        self.assertTrue(gitlink.hook_line("post-commit", wd).endswith(" git pin"))
        self.assertTrue(
            gitlink.hook_line("post-checkout", wd).endswith(' git post-checkout "$@"')
        )
        self.assertTrue(
            gitlink.append_line("pre-commit", wd).endswith(" git pre-commit || exit $?")
        )
        self.assertNotIn("exit", gitlink.append_line("post-commit", wd))

    def test_bvc_command_in_development(self):
        # I-13: pyz から実行していなければ python -m bvc
        self.assertEqual(gitlink.bvc_command(), "python -m bvc")

    def test_g6_install_hooks_keeps_existing(self):
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n")
        r = gitlink.install_hooks(hooks, "", "python -m bvc")
        self.assertTrue(r.changed)
        self.assertEqual(r.installed, ["post-commit", "post-checkout"])
        self.assertEqual(list(r.manual), ["pre-commit"])
        self.assertEqual((hooks / "pre-commit").read_text(), "#!/bin/sh\necho mine\n")
        self.assertIn(" git pre-commit || exit $?", r.manual["pre-commit"])
        self.assertIn(
            gitlink.HOOK_MARK, (hooks / "post-commit").read_text(encoding="utf-8")
        )
        # 2回目は設置済み。追記済みの既存フックも設置済みとみなす
        with open(hooks / "pre-commit", "a") as f:
            f.write(r.manual["pre-commit"] + "\n")
        r2 = gitlink.install_hooks(hooks, "", "python -m bvc")
        self.assertFalse(r2.changed)
        self.assertEqual(r2.already, list(gitlink.HOOK_NAMES))
        self.assertEqual(r2.manual, {})

    def test_install_hooks_replaces_absolute_dir(self):
        # LEGACY-HOOK-C(削除予定): このテストごと削除する
        # 以前の版は作業フォルダの絶対パスを -C に書いていた。再実行で相対パスに直し、他の内容は変えない
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        legacy = 'python "C:/tools/bvc.pyz" -C "C:/old dir/w\\"x" git'
        (hooks / "pre-commit").write_bytes(
            f"#!/bin/sh\n{gitlink.HOOK_MARK}\nexec {legacy} pre-commit\r\n".encode()
        )
        (hooks / "post-commit").write_text(
            f"#!/bin/sh\necho mine\n{legacy} pin\n", encoding="utf-8"
        )
        (hooks / "post-checkout").write_text(
            f'#!/bin/sh\npython -m bvc -C "/home/u/w" git post-checkout "$@"\n',
            encoding="utf-8",
        )
        r = gitlink.install_hooks(hooks, "sub", "python -m bvc")
        self.assertEqual(r.updated, list(gitlink.HOOK_NAMES))
        self.assertTrue(r.changed)
        self.assertEqual(r.already, list(gitlink.HOOK_NAMES))
        self.assertEqual(r.manual, {})
        self.assertEqual(
            (hooks / "pre-commit").read_bytes(),
            f'#!/bin/sh\n{gitlink.HOOK_MARK}\nexec python "C:/tools/bvc.pyz" -C "sub" git pre-commit\r\n'.encode(),
        )
        self.assertEqual(
            (hooks / "post-commit").read_text(encoding="utf-8"),
            '#!/bin/sh\necho mine\npython "C:/tools/bvc.pyz" -C "sub" git pin\n',
        )
        self.assertIn(
            'python -m bvc -C "sub" git post-checkout "$@"',
            (hooks / "post-checkout").read_text(encoding="utf-8"),
        )
        # 2回目は何もしない
        r2 = gitlink.install_hooks(hooks, "sub", "python -m bvc")
        self.assertEqual((r2.changed, r2.updated), (False, []))

    def test_install_hooks_creates_folder(self):
        r = gitlink.install_hooks(self.tmp / "a" / "hooks")
        self.assertEqual(r.installed, list(gitlink.HOOK_NAMES))
        self.assertTrue((self.tmp / "a" / "hooks" / "post-checkout").is_file())


@unittest.skipUnless(shutil.which("git"), "git がありません")
class TestGit(helpers.TempDirTestCase):
    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.tmp,
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        ).stdout

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.g = gitlink.Git(self.tmp)

    def commit(self, content: bytes | None, msg: str):
        p = self.tmp / "bvc.lock"
        if content is None:
            self.git("rm", "-q", "--cached", "--ignore-unmatch", "bvc.lock")
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(content)
            self.git("add", "bvc.lock")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def test_basic(self):
        self.assertTrue(self.g.is_work_tree())
        self.assertIsNone(self.g.head())
        self.assertTrue(
            str(self.g.hooks_dir()).replace("\\", "/").endswith(".git/hooks")
        )
        self.commit(b"v1", "1")
        head = self.g.head()
        self.assertRegex(head, "^[0-9a-f]{40}$")
        self.assertEqual(self.g.blob_at(head, "bvc.lock"), b"v1")
        self.assertIsNone(self.g.blob_at(head, "other"))
        self.assertEqual(self.g.staged("bvc.lock"), b"v1")
        (self.tmp / "bvc.lock").write_bytes(b"v2")
        self.assertEqual(self.g.staged("bvc.lock"), b"v1")
        self.g.add("bvc.lock")
        self.assertEqual(self.g.staged("bvc.lock"), b"v2")

    def test_history_blobs_covers_branches_stash_and_reflog(self):
        self.commit(b"v1", "1")
        self.commit(b"v2", "2")
        self.git("checkout", "-q", "-b", "side", "HEAD~1")
        self.commit(b"side", "s")
        self.git("checkout", "-q", "-")
        self.commit(None, "removed")
        self.commit(b"v3", "3")
        self.git("reset", "-q", "--hard", "HEAD~1")  # v3 は reflog にだけ残る
        (self.tmp / "bvc.lock").write_bytes(b"stashed")
        self.git("add", "bvc.lock")
        self.git("stash", "-q")
        (self.tmp / "bvc.lock").write_bytes(b"staged")
        self.git("add", "bvc.lock")
        blobs = self.g.history_blobs("bvc.lock")
        self.assertEqual(
            sorted(self.g.cat_blobs(blobs)),
            sorted([b"v1", b"v2", b"side", b"v3", b"stashed", b"staged"]),
        )

    def test_failures(self):
        with self.assertRaises(GitFailed):
            self.g.run("no-such-command")
        with self.assertRaises(GitFailed):
            self.g.cat_blobs(["0" * 39 + "1"])
        other = helpers.make_temp_dir()
        self.addCleanup(helpers.remove_tree, other)
        self.assertFalse(gitlink.Git(other).is_work_tree())
        self.assertFalse(gitlink.Git(other / "missing").is_work_tree())


if __name__ == "__main__":
    unittest.main()
