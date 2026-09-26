# store: チャンク・マニフェスト・put_file/write_file・隔離・health・全件検証(M1-11〜M1-15)。

import hashlib
import io
import json
import os
import tracemalloc
import unittest
import zlib
from pathlib import Path
from typing import Any, Final
from unittest import mock

from bvc import chunkers, fsutil, store
from bvc.errors import CorruptData, UnsafePath, UnsupportedFormat
from bvc.model import ChunkRef, Manifest
from bvc.store import Health, ObjectStore
from tests import helpers

FIXED: Final[dict[str, Any]] = {"name": "fixed", "size": 1000}
WHOLE: Final[dict[str, Any]] = {"name": "whole"}


class StoreTestCase(helpers.TempDirTestCase):
    compression = "auto"
    threads = 2

    def setUp(self):
        super().setUp()
        self.repodir = self.tmp / ".bvc"
        self.store = self.open_store()

    def open_store(self, **kw):
        kw.setdefault("compression", self.compression)
        kw.setdefault("threads", self.threads)
        s = ObjectStore(self.repodir, **kw)
        self.addCleanup(s.close)
        return s

    def put(self, data, chunker=FIXED, check="exists", s=None):
        return (s or self.store).put_file(io.BytesIO(data), chunker, check)

    def read(self, sha, s=None):
        out = io.BytesIO()
        (s or self.store).write_file(sha, out)
        return out.getvalue()

    def chunk_files(self):
        return sorted(p.name for p in (self.repodir / "chunks").glob("*/*"))

    def quarantined(self, kind="chunks"):
        d = self.repodir / "quarantine" / kind
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


class TestRoundtrip(StoreTestCase):
    def test_put_write(self):
        for compression in ("none", "zlib", "auto"):
            s = self.open_store(compression=compression)
            for chunker in (FIXED, WHOLE):
                for n in (0, 1, 999, 1000, 1001, 5555):
                    for data in (helpers.random_bytes(n, n), bytes(n)):
                        with self.subTest(compression=compression, chunker=chunker["name"], n=n):
                            sha, stats = self.put(data, chunker, s=s)
                            self.assertEqual(self.read(sha, s), data)
                            self.assertEqual(stats.size, n)
                            m = s.get_manifest(sha)
                            self.assertEqual(m.size, n)
                            self.assertEqual(m.sha256, hashlib.sha256(data).hexdigest())
                            self.assertEqual(m.chunker, chunker)
                            self.assertTrue(s.manifest_ok(sha, "full"))

    def test_empty_file(self):
        sha, stats = self.put(b"")
        self.assertEqual(self.store.get_manifest(sha).chunks, ())
        self.assertEqual(stats.chunks, 0)
        self.assertEqual(self.read(sha), b"")

    def test_manifest_name_is_canonical_json_sha(self):
        sha, _ = self.put(helpers.random_bytes(2500))
        data = self.store.manifest_path(sha).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
        obj = json.loads(data)
        self.assertEqual(fsutil.canonical_json(obj), data)
        self.assertEqual(obj["format"], 1)
        self.assertEqual([c[1] for c in obj["chunks"]], [1000, 1000, 500])

    def test_hash_file_matches(self):
        for chunker in (FIXED, WHOLE):
            data = helpers.random_bytes(3333, 7)
            sha, _ = self.put(data, chunker)
            self.assertEqual(self.store.hash_file(io.BytesIO(data), chunker), sha)

    def test_dedupe(self):
        data = helpers.random_bytes(5000, 1)
        sha1, st1 = self.put(data)
        files = self.chunk_files()
        sha2, st2 = self.put(data)
        self.assertEqual(sha1, sha2)
        self.assertEqual((st1.chunks, st1.new_chunks), (5, 5))
        self.assertEqual((st2.chunks, st2.new_chunks, st2.new_bytes), (5, 0, 0))
        self.assertEqual(self.chunk_files(), files)
        # 同じ内容のチャンクが1ファイルの中で繰り返される場合
        sha3, st3 = self.put(data[:1000] * 4)
        self.assertEqual(st3.new_chunks, 0)
        self.assertEqual(self.read(sha3), data[:1000] * 4)

    def test_order_with_many_threads(self):
        s = self.open_store(threads=8)
        data = helpers.random_bytes(200_000, 9)
        sha, stats = self.put(data, {"name": "fixed", "size": 1024}, s=s)
        self.assertEqual(stats.chunks, 196)
        self.assertEqual(self.read(sha, s), data)

    def test_stream_path_for_large_chunk(self):
        # STREAM_THRESHOLD を超えるチャンクは逐次保存になる。結果は同じ
        data = helpers.random_bytes(10_000, 4) + bytes(10_000)
        with mock.patch.object(store, "STREAM_THRESHOLD", 2000), \
                mock.patch.object(chunkers, "READ_SIZE", 700):
            sha, stats = self.put(data, {"name": "fixed", "size": 6000})
            self.assertEqual(stats.chunks, 4)
            self.assertEqual(self.read(sha), data)
            self.assertEqual(self.store.hash_file(io.BytesIO(data), {"name": "fixed", "size": 6000}), sha)
        # 同じ内容を普通の経路で入れると、同じマニフェストになり、チャンクも増えない
        sha2, stats2 = self.put(data, {"name": "fixed", "size": 6000})
        self.assertEqual((sha2, stats2.new_chunks), (sha, 0))

    def test_whole_chunk_sha_is_file_sha(self):
        data = helpers.random_bytes(5000, 3)
        sha, _ = self.put(data, WHOLE)
        m = self.store.get_manifest(sha)
        self.assertEqual(len(m.chunks), 1)
        self.assertEqual(m.chunks[0].sha, m.sha256)

    def test_whole_none_manual_recovery(self):
        # F-13: whole + none なら、保存データの先頭1バイトを除けば元ファイルに戻せる
        s = self.open_store(compression="none")
        data = helpers.random_bytes(12345, 8)
        sha, _ = self.put(data, WHOLE, s=s)
        whole_sha = s.get_manifest(sha).sha256
        chunk = self.repodir / "chunks" / whole_sha[:2] / whole_sha
        self.assertEqual(chunk.read_bytes()[1:], data)

    def test_progress(self):
        events = []
        data = helpers.random_bytes(3500)
        with open(self.tmp / "f.bin", "wb") as f:
            f.write(data)
        with open(self.tmp / "f.bin", "rb") as f:
            self.store.put_file(f, FIXED, progress=events.append, path="f.bin")
        self.assertEqual(events[-1].done, 3500)
        self.assertEqual(events[-1].total, 3500)
        self.assertEqual(events[-1].path, "f.bin")


class TestCorruption(StoreTestCase):
    compression = "none"

    def setUp(self):
        super().setUp()
        self.data = helpers.random_bytes(3000, 11)
        self.msha, _ = self.put(self.data)
        self.m = self.store.get_manifest(self.msha)
        self.victim = self.m.chunks[1].sha
        self.victim_path = self.store.chunk_path(self.victim)

    def assert_detected(self, reason):
        with self.assertRaises(CorruptData):
            self.read(self.msha)
        self.assertEqual(self.store.health.records("bad_chunks")[self.victim]["reason"], reason)
        self.assertFalse(self.store.has_chunk(self.victim))
        self.assertFalse(self.store.manifest_ok(self.msha))
        # health.json に書かれ、開き直しても引き継がれる
        self.assertTrue(Health(self.repodir).is_bad("bad_chunks", self.victim))

    def assert_repaired_by_put(self):
        # C-5: 隔離済みのチャンクは重複排除で再利用されず、put で作り直される
        _, stats = self.put(self.data)
        self.assertEqual(stats.new_chunks, 1)
        self.assertEqual(self.read(self.msha), self.data)
        self.assertFalse(self.store.health.is_bad("bad_chunks", self.victim))
        self.assertTrue(self.store.manifest_ok(self.msha, "full"))

    def test_deleted_chunk(self):
        os.remove(self.victim_path)
        self.assert_detected("missing")
        self.assert_repaired_by_put()

    def test_flipped_byte(self):
        helpers.flip_byte(self.victim_path, 500)
        # exists の検査では見逃す(4.11節のトレードオフ)が、full なら見つける
        self.assertTrue(self.store.has_chunk(self.victim, "exists"))
        self.assertFalse(self.store.has_chunk(self.victim, "full"))
        self.assertEqual(self.quarantined(), [self.victim])
        self.assertFalse(self.victim_path.exists())
        self.assert_detected("hash_mismatch")
        self.assert_repaired_by_put()

    def test_flipped_byte_detected_on_read(self):
        helpers.flip_byte(self.victim_path, 1)
        self.assert_detected("hash_mismatch")
        self.assertEqual(self.quarantined(), [self.victim])

    def test_truncated(self):
        helpers.truncate_file(self.victim_path, 500)
        self.assert_detected("length_mismatch")
        self.assert_repaired_by_put()

    def test_truncated_to_empty(self):
        helpers.truncate_file(self.victim_path, 0)
        self.assertFalse(self.store.has_chunk(self.victim))
        self.assert_detected("no_header")
        self.assert_repaired_by_put()

    def test_codec_id_swapped(self):
        # raw → zlib に書き換えると復号できない
        helpers.set_first_byte(self.victim_path, 1)
        self.assert_detected("decode_error")
        self.assert_repaired_by_put()

    def test_unknown_codec_id(self):
        # V-2: 知らない codec ID は壊れたデータと区別し、何も書き込まずに中止する
        helpers.set_first_byte(self.victim_path, 0x7F)
        before = self.victim_path.read_bytes()
        for op in (lambda: self.read(self.msha),
                   lambda: self.store.has_chunk(self.victim),
                   lambda: self.put(self.data)):
            with self.assertRaises(UnsupportedFormat):
                op()
        self.assertEqual(self.victim_path.read_bytes(), before)
        self.assertFalse((self.repodir / "quarantine").exists())
        self.assertFalse((self.repodir / "health.json").exists())

    def test_fake_content(self):
        # C-11: 別の正しい形式のデータに差し替えても(名前と合わない)、照合で見つかる
        other = helpers.random_bytes(1000, 99)
        self.victim_path.write_bytes(b"\x00" + other)
        self.assert_detected("hash_mismatch")

    def test_fake_content_zlib(self):
        other = helpers.random_bytes(1000, 99)
        self.victim_path.write_bytes(b"\x01" + zlib.compress(other))
        self.assert_detected("hash_mismatch")

    def test_zip_bomb_chunk(self):
        # C-9: 小さなチャンクを展開すると巨大になるデータに差し替えても、上限で止まる
        self.victim_path.write_bytes(b"\x01" + zlib.compress(bytes(200 << 20), 9))
        tracemalloc.start()
        try:
            with self.assertRaises(CorruptData):
                self.store.get_chunk(self.victim, 1000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 << 20)
        self.assertEqual(self.store.health.records("bad_chunks")[self.victim]["reason"], "decode_error")

    def test_read_error_not_quarantined(self):
        # 読み込みの OSError(使用中など)は破損の証拠ではないので、隔離しない(D-15)
        class Failing:
            def iter_decode(self, pieces, limit):
                raise PermissionError("使用中")
                yield

        with mock.patch.object(store, "get_codec", return_value=Failing()):
            with self.assertRaises(PermissionError):
                self.store.get_chunk(self.victim, 1000)
        with mock.patch.object(fsutil, "read_bytes", side_effect=PermissionError("使用中")):
            with self.assertRaises(PermissionError):
                self.store.get_manifest(self.msha)
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.quarantined("manifests"), [])
        self.assertEqual(self.store.health.records("bad_chunks"), {})
        self.assertEqual(self.read(self.msha), self.data)

    def test_quarantine_name_collision(self):
        helpers.flip_byte(self.victim_path, 1)
        self.assertFalse(self.store.has_chunk(self.victim, "full"))
        self.put(self.data)
        helpers.flip_byte(self.victim_path, 1)
        self.assertFalse(self.store.has_chunk(self.victim, "full"))
        q = self.quarantined()
        self.assertEqual(len(q), 2)
        self.assertTrue(all(n.startswith(self.victim) for n in q))


class TestManifestCorruption(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.data = helpers.random_bytes(3000, 12)
        self.msha, _ = self.put(self.data)
        self.path = self.store.manifest_path(self.msha)

    def write_named(self, obj):
        # 内容のハッシュに名前を合わせたマニフェストを置く(改ざんの再現)。
        data = fsutil.canonical_json(obj)
        sha = hashlib.sha256(data).hexdigest()
        p = self.store.manifest_path(sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return sha

    def test_tampered(self):
        helpers.break_json(self.path)
        with self.assertRaises(CorruptData):
            self.store.get_manifest(self.msha)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.quarantined("manifests"), [f"{self.msha}.json"])
        self.assertFalse(self.store.manifest_ok(self.msha))
        # put で作り直される
        sha, _ = self.put(self.data)
        self.assertEqual(sha, self.msha)
        self.assertEqual(self.read(sha), self.data)
        self.assertFalse(self.store.health.is_bad("bad_manifests", sha))

    def test_missing(self):
        os.remove(self.path)
        with self.assertRaises(CorruptData):
            self.store.get_manifest(self.msha)
        self.assertEqual(self.store.health.records("bad_manifests")[self.msha]["reason"], "missing")

    def test_put_replaces_corrupt_existing(self):
        # 同名のファイルが違う内容なら、隔離して書き直す
        self.path.write_bytes(b"junk")
        sha, _ = self.put(self.data)
        self.assertEqual(self.store.get_manifest(sha).size, 3000)
        self.assertEqual(self.quarantined("manifests"), [f"{self.msha}.json"])

    def test_invalid_fields_with_matching_name(self):
        # P-2: 名前は合っていても、中身の値が不正なら形式エラー。値をパスに使わない
        good = json.loads(self.path.read_bytes())
        c0 = good["chunks"][0]
        variants = {
            "chunk_sha_traversal": {**good, "chunks": [["../" + c0[0][3:], c0[1]]] + good["chunks"][1:]},
            "chunk_sha_upper": {**good, "chunks": [[c0[0].upper(), c0[1]]] + good["chunks"][1:]},
            "chunk_len_zero": {**good, "chunks": [[c0[0], 0]] + good["chunks"][1:]},
            "chunk_len_float": {**good, "chunks": [[c0[0], 1000.0]] + good["chunks"][1:]},
            "size_mismatch": {**good, "size": good["size"] + 1},
            "size_negative": {**good, "size": -1},
            "bad_sha256": {**good, "sha256": "x"},
            "extra_key": {**good, "extra": 1},
            "missing_key": {k: v for k, v in good.items() if k != "chunker"},
            "not_dict": [1, 2],
        }
        for name, obj in variants.items():
            with self.subTest(name):
                sha = self.write_named(obj)
                with self.assertRaises(CorruptData):
                    self.store.get_manifest(sha)
                self.assertTrue(self.store.health.is_bad("bad_manifests", sha))

    def test_unknown_format_not_quarantined(self):
        # V-2: 知らない format は UnsupportedFormat で、隔離もしない
        good = json.loads(self.path.read_bytes())
        sha = self.write_named({**good, "format": 2})
        with self.assertRaises(UnsupportedFormat):
            self.store.get_manifest(sha)
        self.assertTrue(self.store.manifest_path(sha).exists())
        self.assertFalse(self.store.health.is_bad("bad_manifests", sha))

    def test_bad_sha_argument(self):
        # P-2: 不正なハッシュはパスにする前に止まる
        for s in ("../" + "a" * 61, "A" * 64, "a" * 63, 5):
            with self.subTest(s=s):
                with self.assertRaises(UnsafePath):
                    self.store.get_manifest(s)
                with self.assertRaises(UnsafePath):
                    self.store.has_chunk(s)
                with self.assertRaises(UnsafePath):
                    self.store.quarantine("chunk", s, "x")
        self.assertFalse((self.repodir / "quarantine").exists())

    def test_content_mismatch(self):
        # チャンクはどれも健全だが、全体の SHA-256 が合わないマニフェスト
        good = json.loads(self.path.read_bytes())
        sha = self.write_named({**good, "sha256": "0" * 64})
        with self.assertRaises(CorruptData):
            self.read(sha)
        self.assertEqual(self.store.health.records("bad_manifests")[sha]["reason"], "content_mismatch")


class TestHealth(StoreTestCase):
    def test_roundtrip(self):
        h = Health(self.repodir)
        h.mark("bad_chunks", "a" * 64, "missing")
        h.mark("bad_commits", "6", "json_error")
        h2 = Health(self.repodir)
        self.assertTrue(h2.is_bad("bad_chunks", "a" * 64))
        self.assertEqual(h2.records("bad_commits")["6"]["reason"], "json_error")
        h2.clear("bad_chunks", "a" * 64)
        self.assertFalse(Health(self.repodir).is_bad("bad_chunks", "a" * 64))
        self.assertEqual(json.loads((self.repodir / "health.json").read_bytes())["format"], 1)

    def test_corrupt_becomes_empty(self):
        self.repodir.mkdir(exist_ok=True)
        for content in (b"{", b'{"format":1,"bad_chunks":[]}',
                        b'{"format":1,"bad_chunks":{"../x":{"time":"t","reason":"r"}}}',
                        b'{"format":1,"bad_commits":{"-1":{"time":"t","reason":"r"}}}'):
            with self.subTest(content=content):
                (self.repodir / "health.json").write_bytes(content)
                h = Health(self.repodir)
                self.assertEqual(h.records("bad_chunks"), {})
                self.assertEqual(len(h.warnings), 1)
                h.mark("bad_chunks", "b" * 64, "x")  # 次の書き込みで作り直す
                self.assertTrue(Health(self.repodir).is_bad("bad_chunks", "b" * 64))

    def test_unknown_format(self):
        self.repodir.mkdir(exist_ok=True)
        (self.repodir / "health.json").write_bytes(b'{"format":9}')
        with self.assertRaises(UnsupportedFormat):
            Health(self.repodir)
        with self.assertRaises(UnsupportedFormat):
            ObjectStore(self.repodir)


class TestFault(StoreTestCase):
    def test_chunk_write_fault(self):
        # R-1(単体): チャンクの書き込み段階で失敗しても、tmp が残らず、やり直せる
        data = helpers.random_bytes(5000, 13)
        hook = helpers.FaultAt("chunk_write", count=3)
        with mock.patch.object(fsutil, "_fault_hook", hook):
            with self.assertRaises(OSError):
                self.put(data)
        self.assertEqual(list((self.repodir / "tmp").iterdir()), [])
        self.assertEqual(list(self.store.iter_manifests()), [])
        sha, _ = self.put(data)
        self.assertEqual(self.read(sha), data)

    def test_manifest_write_fault(self):
        data = helpers.random_bytes(5000, 14)
        with mock.patch.object(fsutil, "_fault_hook", helpers.FaultAt("atomic_write:", count=1)) as hook:
            hook.stage = None
            self.put(data)
            names = [c for c in hook.calls if c.startswith("atomic_write:")]
        self.assertEqual(len(names), 1)
        self.setUp()  # 別のリポジトリでやり直す
        with mock.patch.object(fsutil, "_fault_hook", helpers.FaultAt(names[0])):
            with self.assertRaises(OSError):
                self.put(data)
        self.assertEqual(list(self.store.iter_manifests()), [])
        self.assertEqual(list((self.repodir / "tmp").iterdir()), [])
        sha, _ = self.put(data)
        self.assertEqual(self.read(sha), data)

    def test_stream_fault(self):
        data = helpers.random_bytes(5000, 15)
        with mock.patch.object(fsutil, "_fault_hook", helpers.FaultAt("chunk_write")):
            with self.assertRaises(OSError):
                self.put(data, WHOLE)
        self.assertEqual(list((self.repodir / "tmp").iterdir()), [])
        self.assertEqual(self.chunk_files(), [])


class TestIterDeleteVerify(StoreTestCase):
    compression = "none"

    def test_iter_and_delete(self):
        sha, _ = self.put(helpers.random_bytes(2500, 16))
        chunks = sorted(c.sha for c in self.store.get_manifest(sha).chunks)
        self.assertEqual(sorted(self.store.iter_chunks()), chunks)
        self.assertEqual(list(self.store.iter_manifests()), [sha])
        # 名前の形式が違うファイルは無視する
        (self.repodir / "chunks" / "zz").mkdir()
        (self.repodir / "chunks" / chunks[0][:2] / "garbage").write_bytes(b"")
        (self.repodir / "chunks" / "00" ).mkdir(exist_ok=True)
        (self.repodir / "chunks" / "00" / ("11" + "0" * 62)).write_bytes(b"")  # フォルダ違い
        self.assertEqual(sorted(self.store.iter_chunks()), chunks)
        self.store.delete_chunk(chunks[0])
        self.store.delete_manifest(sha)
        self.assertEqual(sorted(self.store.iter_chunks()), chunks[1:])
        self.assertEqual(list(self.store.iter_manifests()), [])
        self.store.delete_chunk(chunks[0])  # 無くてもよい

    def test_verify_clean(self):
        self.put(helpers.random_bytes(2500, 17))
        self.put(bytes(2500), WHOLE)
        for quick in (False, True):
            res = self.store.verify_all(quick=quick)
            self.assertTrue(res.ok, res)
            self.assertEqual((res.checked_chunks, res.checked_manifests), (4, 2))

    def test_verify_full_finds_flip(self):
        sha, _ = self.put(helpers.random_bytes(2500, 18))
        victim = self.store.get_manifest(sha).chunks[0].sha
        helpers.flip_byte(self.store.chunk_path(victim), 10)
        self.assertTrue(self.store.verify_all(quick=True).ok)  # quick では見逃す
        res = self.store.verify_all()
        self.assertEqual(res.bad_chunks, [victim])
        self.assertEqual(list(res.broken_manifests), [sha])
        self.assertEqual(self.quarantined(), [victim])
        # 隔離後は quick でも欠損として見つかる
        res = self.store.verify_all(quick=True)
        self.assertEqual(list(res.broken_manifests), [sha])

    def test_verify_quick_finds_length_and_missing(self):
        sha, _ = self.put(helpers.random_bytes(2500, 19))
        chunks = self.store.get_manifest(sha).chunks
        helpers.truncate_file(self.store.chunk_path(chunks[0].sha), 100)  # raw なので長さで分かる
        res = self.store.verify_all(quick=True)
        self.assertIn("長さ", res.broken_manifests[sha])
        os.remove(self.store.chunk_path(chunks[1].sha))
        sha2, _ = self.put(helpers.random_bytes(1000, 20))
        res = self.store.verify_all(quick=True)
        self.assertIn(sha, res.broken_manifests)
        self.assertNotIn(sha2, res.broken_manifests)

    def test_verify_bad_manifest(self):
        sha, _ = self.put(helpers.random_bytes(2500, 21))
        helpers.break_json(self.store.manifest_path(sha))
        res = self.store.verify_all()
        self.assertEqual(res.bad_manifests, [sha])


class TestLarge(helpers.TempDirTestCase):
    @helpers.slow
    def test_whole_over_1gb_constant_memory(self):
        # S-3, S-7: 1GB 超の whole を保存・復元しても、メモリ使用量がファイルサイズに比例しない
        size = (1 << 30) + 12345
        src = self.tmp / "big.bin"
        block = helpers.random_bytes(1 << 20, 1)
        h = hashlib.sha256()
        with open(src, "wb") as f:
            remaining = size
            while remaining:
                b = block[: min(len(block), remaining)]
                f.write(b)
                h.update(b)
                remaining -= len(b)
        expected = h.hexdigest()
        del block

        with ObjectStore(self.tmp / ".bvc", compression="none", threads=2) as s:
            tracemalloc.start()
            try:
                with open(src, "rb") as f:
                    sha, stats = s.put_file(f, WHOLE)
                _, peak_put = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                out_path = self.tmp / "restored.bin"
                with open(out_path, "wb") as out:
                    s.write_file(sha, out)
                _, peak_write = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(stats.size, size)
        self.assertEqual(helpers.sha256_file(out_path), expected)
        # 読み込み単位の定数倍(先読みの断片、読み込み中の断片、書き込み中の断片など)に収まる。
        # 1GB のファイルに対して 128MiB で、ファイルサイズには比例しない
        limit = 8 * chunkers.READ_SIZE
        self.assertLess(peak_put, limit)
        self.assertLess(peak_write, limit)


class TestM4C(StoreTestCase):
    # M4-9〜M4-11 で加えたもの: health の異常の記録、verify_all の欠損の記録、peek_manifest、repair_from。
    compression = "none"

    def test_health_problem_and_rebuild(self):
        self.repodir.mkdir(exist_ok=True)
        self.assertEqual(Health(self.repodir).problem, "missing")
        (self.repodir / "health.json").write_bytes(b"{")
        h = Health(self.repodir)
        self.assertEqual(h.problem, "corrupt")
        h.rebuild()
        self.assertIsNone(h.problem)
        h2 = Health(self.repodir)
        self.assertEqual((h2.problem, h2.records("bad_chunks")), (None, {}))

    def test_verify_all_records_missing_and_length_mismatch(self):
        sha, _ = self.put(helpers.random_bytes(2500, 30))
        chunks = self.store.get_manifest(sha).chunks
        os.remove(self.store.chunk_path(chunks[0].sha))
        helpers.truncate_file(self.store.chunk_path(chunks[1].sha), 10)
        res = self.store.verify_all(quick=True)
        self.assertIn(sha, res.broken_manifests)
        self.assertTrue(self.store.health.is_bad("bad_chunks", chunks[0].sha))
        self.assertEqual(self.store.health.records("bad_chunks")[chunks[0].sha]["reason"], "missing")
        # 1つのマニフェストでは最初の異常で止まるので、長さ違いは別のマニフェストで確かめる
        m2 = Manifest(size=chunks[1].length, sha256=hashlib.sha256(b"").hexdigest(), chunker=FIXED,
                      chunks=(chunks[1],))
        self.store.put_manifest(m2)
        self.store.verify_all(quick=True)
        self.assertEqual(self.store.health.records("bad_chunks")[chunks[1].sha]["reason"], "length_mismatch")
        self.assertIn(chunks[1].sha, self.quarantined())

    def test_peek_manifest_has_no_side_effects(self):
        sha, _ = self.put(b"abc")
        self.assertEqual(self.store.peek_manifest(sha), self.store.get_manifest(sha))
        helpers.flip_byte(self.store.manifest_path(sha))
        self.assertIsNone(self.store.peek_manifest(sha))
        self.assertIsNone(self.store.peek_manifest("c" * 64))
        self.assertEqual((self.quarantined("manifests"), self.store.health.records("bad_manifests")), ([], {}))

    def test_repair_from(self):
        data = helpers.random_bytes(2500, 31)
        sha, _ = self.put(data)
        chunks = self.store.get_manifest(sha).chunks
        helpers.flip_byte(self.store.chunk_path(chunks[1].sha))
        self.assertFalse(self.store.manifest_ok(sha, "full"))  # 検出して隔離
        os.remove(self.store.manifest_path(sha))
        want_c, want_m = {chunks[1].sha, "d" * 64}, {sha}
        # 分割方式が違えば見つからない
        self.assertEqual(self.store.repair_from(io.BytesIO(data), WHOLE, want_c, want_m), (set(), set()))
        got = self.store.repair_from(io.BytesIO(data), FIXED, want_c, want_m)
        self.assertEqual(got, ({chunks[1].sha}, {sha}))
        self.assertTrue(self.store.manifest_ok(sha, "full"))
        self.assertEqual(self.read(sha), data)
        self.assertEqual(self.store.health.records("bad_chunks"), {})


if __name__ == "__main__":
    unittest.main()
