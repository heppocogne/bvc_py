# fsutil: 原子的書き込み・JSONL・ロック・検査関数・長いパス・glob・障害注入(M1-3〜M1-8)。

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from bvc import fsutil
from bvc.errors import CorruptData, Locked, UnsafePath, UnsupportedFormat
from tests import helpers


class TestAtomicWrite(helpers.TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = self.tmp / "repo" / "tmp"
        self.path = self.tmp / "repo" / "HEAD.json"

    def test_write_and_replace(self):
        fsutil.atomic_write(self.path, b"one", self.tmpdir)
        fsutil.atomic_write(self.path, b"two", self.tmpdir)
        self.assertEqual(self.path.read_bytes(), b"two")
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_fault_keeps_original(self):
        # 置き換えの直前に障害が起きても、元のファイルが残り、tmp も残らない
        fsutil.atomic_write(self.path, b"original", self.tmpdir)
        hook = helpers.FaultAt("atomic_write:HEAD.json")
        with mock.patch.object(fsutil, "_fault_hook", hook):
            with self.assertRaises(OSError):
                fsutil.atomic_write(self.path, b"new", self.tmpdir)
        self.assertEqual(hook.calls, ["atomic_write:HEAD.json"])
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_json(self):
        fsutil.atomic_write_json(self.path, {"format": 1, "b": "日本", "a": [1, 2]}, self.tmpdir)
        self.assertEqual(self.path.read_bytes(), '{"a":[1,2],"b":"日本","format":1}\n'.encode())
        self.assertEqual(fsutil.load_json(self.path, "x")["b"], "日本")

    def test_canonical_json_rejects_nan(self):
        with self.assertRaises(ValueError):
            fsutil.canonical_json({"x": float("nan")})

    def test_load_json_errors(self):
        with self.assertRaises(FileNotFoundError):
            fsutil.load_json(self.path, "x")
        self.path.parent.mkdir(parents=True)
        for content in (b"{", b"\xff", b"[1]", b'{"a":1}', b'{"format":"1"}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(CorruptData):
                    fsutil.load_json(self.path, "x")
        # V-2: 知らない format は壊れたデータと区別する
        self.path.write_bytes(b'{"format":2}')
        with self.assertRaises(UnsupportedFormat):
            fsutil.load_json(self.path, "x")

    def test_load_json_oserror_is_not_corruption(self):
        # D-15: 読めない(使用中など)ことを破損と誤認して、自動復旧に進ませない
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"format":1}')
        with mock.patch.object(fsutil, "read_bytes", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                fsutil.load_json(self.path, "x")

    def test_load_json_is_a_directory(self):
        # 中身の読めないもの(フォルダ)も、CorruptData ではなく OSError
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            fsutil.load_json(self.path, "x")


class TestJsonl(helpers.TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "oplog.jsonl"

    def test_append_and_read(self):
        for i in range(3):
            fsutil.append_jsonl(self.path, {"format": 1, "i": i})
        records, warnings = fsutil.read_jsonl(self.path, "oplog")
        self.assertEqual([r["i"] for r in records], [0, 1, 2])
        self.assertEqual(warnings, [])

    def test_missing_file(self):
        self.assertEqual(fsutil.read_jsonl(self.path, "oplog"), ([], []))

    def test_broken_last_line_ignored(self):
        # C-8: 書き込み途中で終わった最終行は黙って無視する
        fsutil.append_jsonl(self.path, {"format": 1, "i": 0})
        with open(self.path, "ab") as f:
            f.write(b'{"format":1,"i"')
        records, warnings = fsutil.read_jsonl(self.path, "oplog")
        self.assertEqual([r["i"] for r in records], [0])
        self.assertEqual(warnings, [])

    def test_append_after_broken_last_line(self):
        # 壊れた最終行の後に追記しても、新しい行が壊れた行とつながらない
        fsutil.append_jsonl(self.path, {"format": 1, "i": 0})
        with open(self.path, "ab") as f:
            f.write(b'{"format":1,"i"')
        fsutil.append_jsonl(self.path, {"format": 1, "i": 2})
        records, warnings = fsutil.read_jsonl(self.path, "oplog")
        self.assertEqual([r["i"] for r in records], [0, 2])
        self.assertEqual(len(warnings), 1)  # 途中の行になったので警告する

    def test_broken_middle_lines_skipped_with_warning(self):
        # C-8: 途中の壊れた行は読み飛ばして警告
        lines = [
            b'{"format":1,"i":0}',
            b"garbage",
            b'{"i":1}',  # format が無い
            b"\xff\xfe",
            b"[1,2]",
            b'{"format":1,"i":5}',
        ]
        self.path.write_bytes(b"\n".join(lines) + b"\n")
        records, warnings = fsutil.read_jsonl(self.path, "notes")
        self.assertEqual([r["i"] for r in records], [0, 5])
        self.assertEqual(len(warnings), 4)
        self.assertIn("2行目", warnings[0])

    def test_unknown_format_aborts(self):
        # 設計書 2.6節: 知らない format の行は読み飛ばさずに中止する
        self.path.write_bytes(b'{"format":1}\n{"format":2}\n')
        with self.assertRaises(UnsupportedFormat):
            fsutil.read_jsonl(self.path, "oplog")

    def test_append_does_not_rewrite(self):
        # K-2: 既存の内容は書き換えない
        fsutil.append_jsonl(self.path, {"format": 1, "i": 0})
        before = self.path.read_bytes()
        fsutil.append_jsonl(self.path, {"format": 1, "i": 1})
        self.assertTrue(self.path.read_bytes().startswith(before))

    def test_fault_before_append(self):
        hook = helpers.FaultAt("append_jsonl:oplog.jsonl")
        with mock.patch.object(fsutil, "_fault_hook", hook):
            with self.assertRaises(OSError):
                fsutil.append_jsonl(self.path, {"format": 1})
        self.assertFalse(self.path.exists())


class TestLock(helpers.TempDirTestCase):
    def test_double_acquire(self):
        # R-7(単体): 2つ目の取得は Locked で止まり、1つ目の内容を返す
        path = self.tmp / "lock"
        with fsutil.FileLock(path) as lock:
            self.assertTrue(lock.held)
            info = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(info["pid"], os.getpid())
            for key in ("format", "time", "host"):
                self.assertIn(key, info)
            with self.assertRaises(Locked) as cm:
                fsutil.FileLock(path).acquire()
            self.assertEqual(cm.exception.details["info"]["pid"], os.getpid())
            self.assertIn(str(path), cm.exception.message)
        self.assertFalse(path.exists())
        fsutil.FileLock(path).acquire()  # 解放後は取れる

    def test_stale_lock_is_not_removed(self):
        # 古いロックかどうかは判定しない。壊れたロックファイルでも止まり、消さない
        path = self.tmp / "lock"
        path.write_bytes(b"\x00garbage")
        with self.assertRaises(Locked) as cm:
            fsutil.FileLock(path).acquire()
        self.assertEqual(cm.exception.details["info"], {})
        self.assertTrue(path.exists())

    def test_release_only_own(self):
        path = self.tmp / "lock"
        path.write_text("{}")
        lock = fsutil.FileLock(path)
        lock.release()  # 取得していなければ消さない
        self.assertTrue(path.exists())


class TestChecks(unittest.TestCase):
    def test_check_sha(self):
        # P-2
        good = "0123456789abcdef" * 4
        self.assertEqual(fsutil.check_sha(good), good)
        bad = [good.upper(), good[:-1], good + "0", "../" + good[3:], "g" * 64, "", None,
               64, good[:-1] + "\n", good + "\n", b"a" * 64]
        for s in bad:
            with self.subTest(s=s):
                with self.assertRaises(UnsafePath):
                    fsutil.check_sha(s)

    def test_check_id(self):
        # P-2: 負数、巨大な数、文字列、小数
        for n in (0, 1, 10**15):
            self.assertEqual(fsutil.check_id(n), n)
        for n in (-1, 10**15 + 1, 10**100, "1", 1.0, True, None):
            with self.subTest(n=n):
                with self.assertRaises(UnsafePath):
                    fsutil.check_id(n)

    def test_check_id_str(self):
        self.assertEqual(fsutil.check_id_str("0"), 0)
        self.assertEqual(fsutil.check_id_str("42"), 42)
        for s in ("", "-1", "01", "1.0", " 1", "1\n", "\u0661", "1" * 17, 1):
            with self.subTest(s=s):
                with self.assertRaises(UnsafePath):
                    fsutil.check_id_str(s)

    def test_check_relpath_ok(self):
        for p in ("a.bin", "mesh/a.dat", "日本語/ファイル.bin", ".hidden", "a b/c", "x.bvc/y",
                  "bvc/a", "CONSOLE.bin", "a..b"):
            with self.subTest(p=p):
                self.assertEqual(fsutil.check_relpath(p), p)

    def test_check_relpath_nfc(self):
        nfd = "\u30cf\u309a.bin"  # パ(NFD)
        self.assertEqual(fsutil.check_relpath(nfd), "\u30d1.bin")

    def test_check_relpath_bad(self):
        # P-1(単体): 仕様書 2.9節の規則
        bad = [
            "", "/abs", "../x", "a/../x", "a/..", "./a", "a/./b", "a//b", "a/", "C:", "C:/x",
            "C:\\x", "\\\\server\\x", "a\\b", ".bvc", ".bvc/config.json", ".BVC/x",
            "CON", "con.txt", "a/NUL", "COM1.bin", "LPT9", "com\u00b9", "a.", "a ", "a./b",
            "a<b", "a>b", "a\"b", "a|b", "a?b", "a*b", "a\x00b", "a\x1fb", "a\x7fb", "a\nb",
            None, 1, b"a",
        ]
        for p in bad:
            with self.subTest(p=p):
                with self.assertRaises(UnsafePath):
                    fsutil.check_relpath(p)


class TestResolveInWorkdir(helpers.TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def test_ok(self):
        self.assertEqual(fsutil.resolve_in_workdir(self.work, "a/b.bin"), self.work / "a" / "b.bin")
        (self.work / "a").mkdir()
        (self.work / "a" / "b.bin").write_bytes(b"x")
        self.assertEqual(fsutil.resolve_in_workdir(self.work, "a/b.bin"), self.work / "a" / "b.bin")

    def test_bad_relpath(self):
        for p in ("../x", ".bvc/x", "C:/x"):
            with self.assertRaises(UnsafePath):
                fsutil.resolve_in_workdir(self.work, p)

    def test_file_in_middle(self):
        (self.work / "a").write_bytes(b"x")
        with self.assertRaises(UnsafePath):
            fsutil.resolve_in_workdir(self.work, "a/b.bin")

    def test_symlink_in_middle(self):
        if not helpers.try_symlink(self.outside, self.work / "link", target_is_directory=True):
            self.skipTest("シンボリックリンクを作れない環境")
        with self.assertRaises(UnsafePath):
            fsutil.resolve_in_workdir(self.work, "link/x.bin")

    def test_symlink_target(self):
        (self.outside / "x.bin").write_bytes(b"x")
        if not helpers.try_symlink(self.outside / "x.bin", self.work / "x.bin"):
            self.skipTest("シンボリックリンクを作れない環境")
        with self.assertRaises(UnsafePath):
            fsutil.resolve_in_workdir(self.work, "x.bin")

    @unittest.skipUnless(os.name == "nt", "Windows 固有")
    def test_junction_in_middle(self):
        if not helpers.try_junction(self.outside, self.work / "junc"):
            self.skipTest("ジャンクションを作れない")
        with self.assertRaises(UnsafePath):
            fsutil.resolve_in_workdir(self.work, "junc/x.bin")


class TestLongPath(helpers.TempDirTestCase):
    def test_short_path_unchanged(self):
        with mock.patch.object(fsutil, "LONG_PATH_THRESHOLD", 10**6):
            self.assertEqual(
                fsutil.os_path(self.tmp, "a/b"), os.path.join(str(self.tmp), "a", "b")
            )

    @unittest.skipUnless(os.name == "nt", "Windows 固有")
    def test_prefix(self):
        with mock.patch.object(fsutil, "LONG_PATH_THRESHOLD", 0):
            p = fsutil.os_path(Path("C:/x/y"), "a/b")
            self.assertEqual(p, "\\\\?\\C:\\x\\y\\a\\b")
            self.assertEqual(fsutil.os_path(p), p)  # 二重に付けない
            self.assertEqual(
                fsutil.os_path("\\\\server\\share\\x"), "\\\\?\\UNC\\server\\share\\x"
            )

    @unittest.skipUnless(os.name == "nt", "Windows 固有")
    def test_long_path_io(self):
        # P-6(単体): 260 文字を超えるパスでも読み書き・原子的書き込みができる
        rel = "/".join(["d" * 50] * 6) + "/file.bin"
        path = self.tmp.joinpath(*rel.split("/"))
        self.assertGreater(len(str(path)), 300)
        fsutil.atomic_write(path, b"data", self.tmp / "tmp")
        self.assertEqual(fsutil.read_bytes(path), b"data")
        self.assertEqual(fsutil.resolve_in_workdir(self.tmp, rel), path)
        self.assertTrue(fsutil.same_or_inside(self.tmp, path))

    def test_same_or_inside(self):
        self.assertTrue(fsutil.same_or_inside(self.tmp, self.tmp))
        self.assertTrue(fsutil.same_or_inside(self.tmp, self.tmp / "a" / "b"))
        self.assertFalse(fsutil.same_or_inside(self.tmp / "a", self.tmp / "ab"))
        self.assertFalse(fsutil.same_or_inside(self.tmp / "a", self.tmp))
        self.assertFalse(fsutil.same_or_inside(self.tmp / "a", self.tmp / "a" / ".." / "b"))
        if os.name == "nt":
            self.assertTrue(fsutil.same_or_inside(self.tmp, str(self.tmp).upper() + "\\x"))
        with mock.patch.object(fsutil, "LONG_PATH_THRESHOLD", 0):
            self.assertTrue(fsutil.same_or_inside(self.tmp, self.tmp / "a"))
            self.assertFalse(fsutil.same_or_inside(self.tmp / "a", self.tmp))


class TestGlob(unittest.TestCase):
    def check(self, pattern, yes, no, ignore_case=False):
        for p in yes:
            self.assertTrue(fsutil.glob_match(pattern, p, ignore_case), (pattern, p))
        for p in no:
            self.assertFalse(fsutil.glob_match(pattern, p, ignore_case), (pattern, p))

    def test_star(self):
        self.check("*.bin", ["a.bin", ".bin", "x.y.bin"], ["d/a.bin", "a.bin2", "a.BIN"])

    def test_question(self):
        self.check("a?.dat", ["ab.dat", "a..dat"], ["a.dat", "a/b.dat", "abc.dat"])

    def test_double_star_middle(self):
        self.check("mesh/**/*.dat", ["mesh/a.dat", "mesh/x/a.dat", "mesh/x/y/a.dat"],
                   ["mesh.dat", "a/mesh/a.dat", "mesh/a.bin", "meshx/a.dat"])

    def test_double_star_head_and_tail(self):
        self.check("**/*.bin", ["a.bin", "x/y/a.bin"], ["a.dat"])
        self.check("out/**", ["out/a", "out/x/y"], ["out", "outx/a"])
        self.check("**", ["a", "a/b"], [])

    def test_literal_chars(self):
        self.check("a+(b)[c].bin", ["a+(b)[c].bin"], ["ab.bin", "a+(b)c.bin"])

    def test_case(self):
        self.check("*.BIN", ["a.bin", "A.Bin"], [], ignore_case=True)
        self.check("*.BIN", [], ["a.bin"], ignore_case=False)

    def test_default_case_follows_os(self):
        self.assertEqual(fsutil.glob_match("A.bin", "a.bin"), os.name == "nt")


class TestFaultHook(unittest.TestCase):
    def test_default_none(self):
        self.assertIsNone(fsutil._fault_hook)
        fsutil.fault("anything")  # 何もしない

    def test_replace_stage(self):
        hook = helpers.FaultAt("replace:swap:3")
        with mock.patch.object(fsutil, "_fault_hook", hook):
            with self.assertRaises(OSError):
                fsutil.replace("nonexistent-a", "nonexistent-b", "swap:3")
        self.assertEqual(hook.calls, ["replace:swap:3"])


if __name__ == "__main__":
    unittest.main()
