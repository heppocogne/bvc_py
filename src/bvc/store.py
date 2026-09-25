# チャンク・マニフェストの保存/取得/検証と、health.json の管理。設計書 2.2〜2.3節・2.7節・3.4節。
#
# ファイル名に使うハッシュは、すべて fsutil.check_sha を通してからパスにする(K-3)。
# チャンク・マニフェストは不変で、新規作成のみ(tmp → fsync → os.replace)。

from __future__ import annotations

import concurrent.futures
import hashlib
import itertools
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from bvc import fsutil
from bvc.chunkers import make_chunker
from bvc.codecs import POLICIES, choose_codec, get_codec
from bvc.errors import CorruptData, UnsafePath
from bvc.model import ChunkRef, Manifest, ProgressEvent, PutStats, StoreVerifyResult

# これより大きなチャンクは、メモリに載せずに逐次保存する(whole では常に逐次)
STREAM_THRESHOLD = 64 << 20
# put_file で処理中のチャンクの合計の上限(件数の上限 threads×2 と併用)
MAX_INFLIGHT_BYTES = 256 << 20
# チャンクファイルを読む単位(小さなファイルでも read(n) は n バイトを確保するため、控えめにする)
CHUNK_READ_SIZE = 1 << 20
# 長さが分からないときの復号の上限(逐次処理なのでメモリは使わない)
_NO_LIMIT = 1 << 62

CHECKS = ("exists", "full")
HEALTH_KINDS = ("bad_chunks", "bad_manifests", "bad_commits")

ProgressFn = Callable[[ProgressEvent], None]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_check(check: str) -> None:
    if check not in CHECKS:
        raise ValueError(f"不明な検査の方法: {check!r}")


# ---------------------------------------------------------------------------
# health.json(I-7)
# ---------------------------------------------------------------------------

class Health:
    """health.json(壊れたチャンク・マニフェスト・版の記録)の読み書き。スレッドセーフ。

    壊れていたら空として扱い、次の書き込みで作り直す(警告を warnings に残す)。
    知らない format なら UnsupportedFormat(何も書かない)。
    """

    def __init__(self, repodir: str | os.PathLike[str]) -> None:
        repodir = Path(repodir)
        self.path = repodir / "health.json"
        self.tmpdir = repodir / "tmp"
        self.warnings: list[str] = []
        self._lock = threading.RLock()
        self._data = self._load()

    def _empty(self) -> dict[str, dict[str, dict]]:
        return {k: {} for k in HEALTH_KINDS}

    def _load(self) -> dict[str, dict[str, dict]]:
        try:
            obj = fsutil.load_json(self.path, "health.json")
        except FileNotFoundError:
            return self._empty()
        except CorruptData:
            self.warnings.append("health.json が壊れているため、空として扱います(次の verify で再検出されます)")
            return self._empty()
        try:
            return self._parse(obj)
        except (CorruptData, UnsafePath):
            self.warnings.append("health.json の内容が不正なため、空として扱います(次の verify で再検出されます)")
            return self._empty()

    def _parse(self, obj: dict) -> dict[str, dict[str, dict]]:
        data = self._empty()
        for kind in HEALTH_KINDS:
            entries = obj.get(kind, {})
            if not isinstance(entries, dict):
                raise CorruptData("health.json: 形式が不正です")
            for key, rec in entries.items():
                if kind == "bad_commits":
                    fsutil.check_id_str(key)
                else:
                    fsutil.check_sha(key)
                if not isinstance(rec, dict) or not all(
                    isinstance(rec.get(f), str) for f in ("time", "reason")
                ):
                    raise CorruptData("health.json: 形式が不正です")
                data[kind][key] = {"time": rec["time"], "reason": rec["reason"]}
        return data

    def is_bad(self, kind: str, key: str) -> bool:
        with self._lock:
            return key in self._data[kind]

    def records(self, kind: str) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data[kind].items()}

    def mark(self, kind: str, key: str, reason: str) -> None:
        with self._lock:
            self._data[kind][key] = {"time": fsutil.now_iso(), "reason": reason}
            self._save()

    def clear(self, kind: str, key: str) -> None:
        with self._lock:
            if self._data[kind].pop(key, None) is not None:
                self._save()

    def _save(self) -> None:
        fsutil.atomic_write_json(self.path, {"format": fsutil.FORMAT, **self._data}, self.tmpdir)


# ---------------------------------------------------------------------------
# マニフェストの JSON
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = frozenset(["format", "size", "sha256", "chunker", "chunks"])


def manifest_to_json(m: Manifest) -> dict:
    return {
        "format": fsutil.FORMAT,
        "size": m.size,
        "sha256": m.sha256,
        "chunker": m.chunker,
        "chunks": [[c.sha, c.length] for c in m.chunks],
    }


def manifest_sha(m: Manifest) -> str:
    """マニフェストの名前(正規化 JSON の SHA-256)。"""
    return _sha256(fsutil.canonical_json(manifest_to_json(m)))


def _is_int(v: Any) -> bool:
    return type(v) is int


def manifest_from_json(obj: Any) -> Manifest:
    """JSON の値を検査して Manifest にする。形式が不正なら CorruptData、知らない format なら UnsupportedFormat。"""
    fsutil.check_format(obj, "マニフェスト")

    def bad(why: str) -> CorruptData:
        return CorruptData(f"マニフェストの形式が不正です: {why}")

    if set(obj) != _MANIFEST_KEYS:
        raise bad("項目が違う")
    size, whole_sha, chunker, chunks = obj["size"], obj["sha256"], obj["chunker"], obj["chunks"]
    if not _is_int(size) or size < 0:
        raise bad("size")
    if not isinstance(chunker, dict) or type(chunker.get("name")) is not str:
        raise bad("chunker")
    if not isinstance(chunks, list):
        raise bad("chunks")
    try:
        fsutil.check_sha(whole_sha)
        refs = []
        for c in chunks:
            if not isinstance(c, list) or len(c) != 2 or not _is_int(c[1]) or c[1] <= 0:
                raise bad("chunks の要素")
            refs.append(ChunkRef(fsutil.check_sha(c[0]), c[1]))
    except UnsafePath as e:
        raise bad(e.message) from e
    if sum(r.length for r in refs) != size:
        raise bad("チャンクの長さの合計が size と一致しない")
    return Manifest(size=size, sha256=whole_sha, chunker=chunker, chunks=tuple(refs))


# ---------------------------------------------------------------------------
# 並列処理の補助
# ---------------------------------------------------------------------------

class _Inflight:
    """処理中のチャンクの件数・バイト数を制限する(メモリ使用量を抑えるため)。"""

    def __init__(self, max_count: int, max_bytes: int) -> None:
        self._cond = threading.Condition()
        self._count = 0
        self._bytes = 0
        self._max_count = max_count
        self._max_bytes = max_bytes

    def acquire(self, n: int) -> None:
        with self._cond:
            # 何も処理していなければ、上限を超える1件でも受け付ける
            while self._count > 0 and (
                self._count >= self._max_count or self._bytes + n > self._max_bytes
            ):
                self._cond.wait()
            self._count += 1
            self._bytes += n

    def release(self, n: int) -> None:
        with self._cond:
            self._count -= 1
            self._bytes -= n
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# ObjectStore
# ---------------------------------------------------------------------------

class ObjectStore:
    def __init__(
        self,
        repodir: str | os.PathLike[str],
        compression: str = "auto",
        threads: int = 0,
        health: Health | None = None,
    ) -> None:
        if compression not in POLICIES:
            raise ValueError(f"不明な圧縮方式: {compression!r}")
        if type(threads) is not int or threads < 0:
            raise ValueError(f"threads は 0 以上の整数にしてください: {threads!r}")
        self.repodir = Path(repodir)
        self.tmpdir = self.repodir / "tmp"
        self.compression = compression
        self.threads = threads or os.cpu_count() or 1
        self.health = health if health is not None else Health(self.repodir)
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        # 同じハッシュの確認と書き込みを直列にする(Windows では開いているファイルを置き換えられないため)
        self._stripes = [threading.Lock() for _ in range(64)]
        self._qlock = threading.Lock()

    # --- 後始末 ---

    def close(self) -> None:
        with self._executor_lock:
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None

    def __enter__(self) -> ObjectStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _pool(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.threads, thread_name_prefix="bvc-store"
                )
            return self._executor

    def _stripe(self, sha: str) -> threading.Lock:
        return self._stripes[int(sha[:2], 16) % len(self._stripes)]

    # --- パス ---

    def chunk_path(self, sha: str) -> Path:
        fsutil.check_sha(sha)
        return self.repodir / "chunks" / sha[:2] / sha

    def manifest_path(self, sha: str) -> Path:
        fsutil.check_sha(sha)
        return self.repodir / "manifests" / sha[:2] / f"{sha}.json"

    # --- 隔離 ---

    def quarantine(self, kind: str, sha: str, reason: str) -> None:
        """壊れたチャンク・マニフェストを quarantine/ へ移し、health.json に記録する。

        以後は存在しないものとして扱う(C-5)。ファイルが無ければ記録だけ行う。
        """
        if kind == "chunk":
            src, name, hkind = self.chunk_path(sha), sha, "bad_chunks"
        elif kind == "manifest":
            src, name, hkind = self.manifest_path(sha), f"{sha}.json", "bad_manifests"
        else:
            raise ValueError(f"不明な種類: {kind!r}")
        with self._qlock:
            # 先に記録する(移動の前に中断しても、壊れたデータを健全とみなさない)
            self.health.mark(hkind, sha, reason)
            dstdir = self.repodir / "quarantine" / f"{kind}s"
            dst = dstdir / name
            try:
                fsutil.makedirs(dstdir)
                if os.path.lexists(fsutil.os_path(dst)):
                    dst = dstdir / f"{name}.{uuid.uuid4().hex[:8]}"
                fsutil.replace(src, dst, f"quarantine:{name}")
            except FileNotFoundError:
                pass
            except OSError:
                # 移せなくても記録済みなので、以後は使わない。書き直しのときに置き換える
                pass

    # --- チャンク ---

    def has_chunk(self, sha: str, check: str = "exists", length: int | None = None) -> bool:
        """チャンクが使える状態であるか。

        check="exists": ファイルがあり、ヘッダの codec ID が既知で、隔離記録に無い。
        check="full"  : さらに読み出して復号し、ハッシュ(と length が分かれば長さ)を照合する。
        異常を見つけたら隔離して False を返す。知らない codec ID なら UnsupportedFormat。
        """
        _check_check(check)
        path = self.chunk_path(sha)
        if self.health.is_bad("bad_chunks", sha):
            return False
        try:
            with open(fsutil.os_path(path), "rb") as f:
                head = f.read(1)
        except FileNotFoundError:
            return False
        if not head:
            self.quarantine("chunk", sha, "no_header")
            return False
        get_codec(head[0])
        if check == "exists":
            return True
        try:
            for _ in self._open_verified(sha, length):
                pass
        except CorruptData:
            return False
        return True

    def put_chunk(self, data: bytes, check: str = "exists") -> ChunkRef:
        """チャンクを保存する。has_chunk(sha, check) が False なら書き込む(スレッドセーフ)。

        隔離済み・欠損・破損のチャンクは、ここで作り直される。
        """
        return self._put_chunk(data, check)[0]

    def _put_chunk(self, data: bytes, check: str) -> tuple[ChunkRef, int]:
        """(ChunkRef, 新しく書いたファイルのバイト数。既存なら 0)。"""
        _check_check(check)
        ref = ChunkRef(_sha256(data), len(data))
        with self._stripe(ref.sha):
            if self.has_chunk(ref.sha, check, ref.length):
                return ref, 0
            codec = choose_codec(self.compression, data)
            payload = codec.encode(data)
            tmp = self._write_tmp((bytes([codec.id]), payload))
            self._install_chunk(tmp, ref.sha)
            return ref, 1 + len(payload)

    def put_chunk_stream(
        self, pieces: Iterable[bytes], compression: str | None = None, check: str = "exists"
    ) -> ChunkRef:
        """大きなチャンクを、メモリに載せずに保存する。"""
        return self._put_chunk_stream(pieces, compression, check)[0]

    def _put_chunk_stream(
        self, pieces: Iterable[bytes], compression: str | None, check: str
    ) -> tuple[ChunkRef, int]:
        # tmp に書きながら SHA-256 と圧縮を逐次適用し、最後に has_chunk を確認して、
        # 無ければハッシュ名へ置き換え、あれば tmp を捨てる
        _check_check(check)
        it = iter(pieces)
        first = next(it, b"")
        codec = choose_codec(compression or self.compression, first)
        enc = codec.encoder()
        h = hashlib.sha256()
        total = 0
        stored = 1
        tmp = fsutil.new_tmp_path(self.tmpdir)
        try:
            with open(fsutil.os_path(tmp), "wb") as f:
                f.write(bytes([codec.id]))
                for piece in itertools.chain((first,), it):
                    h.update(piece)
                    total += len(piece)
                    out = enc.update(piece)
                    f.write(out)
                    stored += len(out)
                out = enc.finish()
                f.write(out)
                stored += len(out)
                f.flush()
                os.fsync(f.fileno())
            ref = ChunkRef(h.hexdigest(), total)
            with self._stripe(ref.sha):
                if self.has_chunk(ref.sha, check, ref.length):
                    fsutil.remove_quietly(tmp)
                    return ref, 0
                self._install_chunk(tmp, ref.sha)
            return ref, stored
        except BaseException:
            fsutil.remove_quietly(tmp)
            raise

    def _write_tmp(self, parts: Iterable[bytes]) -> Path:
        tmp = fsutil.new_tmp_path(self.tmpdir)
        try:
            with open(fsutil.os_path(tmp), "wb") as f:
                for p in parts:
                    f.write(p)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            fsutil.remove_quietly(tmp)
            raise
        return tmp

    def _install_chunk(self, tmp: Path, sha: str) -> None:
        """tmp をハッシュ名へ置き換え、隔離記録を消す。失敗したら tmp を消す。"""
        dst = self.chunk_path(sha)
        try:
            fsutil.fault("chunk_write")
            fsutil.makedirs(dst.parent)
            os.replace(fsutil.os_path(tmp), fsutil.os_path(dst))
        except BaseException:
            fsutil.remove_quietly(tmp)
            raise
        fsutil.fsync_dir(dst.parent)
        self.health.clear("bad_chunks", sha)

    def open_chunk(self, sha: str, length: int) -> Iterator[bytes]:
        """チャンクを逐次に復号して断片を返す。

        最後にハッシュと長さを照合し、異常なら隔離して CorruptData を送出する。
        照合の前に断片を返すので、呼び出し側は最後まで読み切ってから結果を確定させること。
        """
        if not _is_int(length) or length < 0:
            raise ValueError(f"length が不正: {length!r}")
        return self._open_verified(sha, length)

    def get_chunk(self, sha: str, length: int) -> bytes:
        """小さなチャンク用。復号後のサイズは length を上限にする(C-9)。"""
        return b"".join(self.open_chunk(sha, length))

    def _open_verified(self, sha: str, length: int | None) -> Iterator[bytes]:
        path = self.chunk_path(sha)
        if self.health.is_bad("bad_chunks", sha):
            raise CorruptData(f"チャンクは壊れているため隔離済みです: {sha}", sha=sha)
        try:
            f = open(fsutil.os_path(path), "rb")
        except FileNotFoundError:
            self.quarantine("chunk", sha, "missing")
            raise CorruptData(f"チャンクがありません: {sha}", sha=sha, reason="missing") from None
        error = None
        h = hashlib.sha256()
        total = 0
        with f:
            head = f.read(1)
            if not head:
                error = "no_header"
            else:
                codec = get_codec(head[0])
                limit = _NO_LIMIT if length is None else length
                try:
                    for out in codec.iter_decode(iter(lambda: f.read(CHUNK_READ_SIZE), b""), limit):
                        h.update(out)
                        total += len(out)
                        yield out
                except CorruptData:
                    error = "decode_error"
                # 読み込みの OSError(使用中など)は破損の証拠ではないので、隔離せずにそのまま送出する(D-15)
        if error is None:
            if length is not None and total != length:
                error = "length_mismatch"
            elif h.hexdigest() != sha:
                error = "hash_mismatch"
        if error is not None:
            self.quarantine("chunk", sha, error)
            raise CorruptData(f"チャンクが壊れています({error}): {sha}", sha=sha, reason=error)

    def iter_chunks(self) -> Iterator[str]:
        """保存されているチャンクの sha(名前の形式が正しいものだけ)。"""
        yield from self._iter_names(self.repodir / "chunks", "")

    def delete_chunk(self, sha: str) -> None:
        fsutil.remove_quietly(self.chunk_path(sha))
        self.health.clear("bad_chunks", sha)

    # --- マニフェスト ---

    def put_manifest(self, m: Manifest) -> str:
        """マニフェストを保存して名前を返す。同名のファイルが同じ内容なら書かない。"""
        data = fsutil.canonical_json(manifest_to_json(m))
        sha = _sha256(data)
        path = self.manifest_path(sha)
        with self._stripe(sha):
            if not self.health.is_bad("bad_manifests", sha):
                try:
                    existing = fsutil.read_bytes(path)
                except FileNotFoundError:
                    existing = None
                if existing == data:
                    return sha
                if existing is not None:
                    self.quarantine("manifest", sha, "hash_mismatch")
            fsutil.atomic_write(path, data, self.tmpdir)
            self.health.clear("bad_manifests", sha)
        return sha

    def get_manifest(self, sha: str) -> Manifest:
        """名前とのハッシュ照合、JSON と値の形式検査をして読む。異常なら隔離して CorruptData。"""
        path = self.manifest_path(sha)
        if self.health.is_bad("bad_manifests", sha):
            raise CorruptData(f"マニフェストは壊れているため隔離済みです: {sha}", sha=sha)

        def fail(reason: str, cause: BaseException | None = None) -> CorruptData:
            self.quarantine("manifest", sha, reason)
            err = CorruptData(f"マニフェストが壊れています({reason}): {sha}", sha=sha, reason=reason)
            err.__cause__ = cause
            return err

        try:
            data = fsutil.read_bytes(path)
        except FileNotFoundError:
            raise fail("missing") from None
        # その他の OSError(使用中など)は破損の証拠ではないので、隔離せずにそのまま送出する(D-15)
        if _sha256(data) != sha:
            raise fail("hash_mismatch")
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise fail("json_error", e) from e
        try:
            return manifest_from_json(obj)
        except CorruptData as e:
            raise fail("invalid", e) from e

    def manifest_ok(self, sha: str, check: str = "exists") -> bool:
        """マニフェストと、その全チャンクが健全か。"""
        _check_check(check)
        try:
            m = self.get_manifest(sha)
            seen: set[str] = set()
            for ref in m.chunks:
                if ref.sha in seen:
                    continue
                seen.add(ref.sha)
                if check == "exists":
                    if not self.has_chunk(ref.sha, "exists"):
                        return False
                else:
                    for _ in self.open_chunk(ref.sha, ref.length):
                        pass
        except CorruptData:
            return False
        return True

    def iter_manifests(self) -> Iterator[str]:
        yield from self._iter_names(self.repodir / "manifests", ".json")

    def delete_manifest(self, sha: str) -> None:
        fsutil.remove_quietly(self.manifest_path(sha))
        self.health.clear("bad_manifests", sha)

    def _iter_names(self, root: Path, suffix: str) -> Iterator[str]:
        try:
            subs = sorted(os.scandir(fsutil.os_path(root)), key=lambda e: e.name)
        except FileNotFoundError:
            return
        for sub in subs:
            if len(sub.name) != 2 or not sub.is_dir(follow_symlinks=False):
                continue
            for e in sorted(os.scandir(sub.path), key=lambda e: e.name):
                name = e.name
                if not name.endswith(suffix):
                    continue
                sha = name[: len(name) - len(suffix)] if suffix else name
                try:
                    fsutil.check_sha(sha)
                except UnsafePath:
                    continue
                if sha[:2] == sub.name and e.is_file(follow_symlinks=False):
                    yield sha

    # --- ファイル単位 ---

    def put_file(
        self,
        f: BinaryIO,
        chunker: dict,
        check: str = "exists",
        progress: ProgressFn | None = None,
        path: str | None = None,
    ) -> tuple[str, PutStats]:
        """ファイルを分割して保存し、(マニフェストの sha, 統計) を返す。

        読み込み・分割・全体の SHA-256 はメインスレッドで行い、チャンクの hash・圧縮・書き込みは
        ワーカーで行う。処理中のチャンクは threads×2 件(かつ MAX_INFLIGHT_BYTES)までに制限する。
        大きなチャンク(STREAM_THRESHOLD 超、whole)はメインスレッドで逐次保存する。
        path は進捗表示用。
        """
        _check_check(check)
        ck = make_chunker(chunker)
        total_size = _file_size(f)
        full = hashlib.sha256()
        done = 0
        slots: list[concurrent.futures.Future | tuple[ChunkRef, int]] = []
        inflight = _Inflight(self.threads * 2, MAX_INFLIGHT_BYTES)
        pool = self._pool()

        def report() -> None:
            if progress is not None:
                progress(ProgressEvent("put", done, total_size, path))

        it = ck.split(f)
        buf: list[bytes] = []
        buflen = 0
        try:
            for piece, end in it:
                full.update(piece)
                done += len(piece)
                buf.append(piece)
                buflen += len(piece)
                if end:
                    data = buf[0] if len(buf) == 1 else b"".join(buf)
                    buf, buflen = [], 0
                    n = len(data)
                    inflight.acquire(n)
                    fut = pool.submit(self._put_chunk, data, check)
                    fut.add_done_callback(lambda _f, n=n: inflight.release(n))
                    slots.append(fut)
                    del data
                    report()
                elif ck.always_stream or buflen > STREAM_THRESHOLD:
                    pending, buf, buflen = buf, [], 0

                    def rest(pending: list[bytes] = pending) -> Iterator[bytes]:
                        nonlocal done
                        yield from pending
                        pending.clear()
                        for piece2, end2 in it:
                            full.update(piece2)
                            done += len(piece2)
                            yield piece2
                            report()
                            if end2:
                                return

                    slots.append(self._put_chunk_stream(rest(), None, check))
                    del pending
                    report()
        except BaseException:
            # 未着手のものは取り消し、実行中のものは終わるまで待つ(戻った後に書き込みが続かないように)
            futs = [s for s in slots if isinstance(s, concurrent.futures.Future)]
            for fut in futs:
                fut.cancel()
            concurrent.futures.wait(futs)
            raise
        concurrent.futures.wait([s for s in slots if isinstance(s, concurrent.futures.Future)])

        stats = PutStats(size=done)
        refs: list[ChunkRef] = []
        for s in slots:
            ref, stored = s.result() if isinstance(s, concurrent.futures.Future) else s
            refs.append(ref)
            stats.chunks += 1
            if stored:
                stats.new_chunks += 1
                stats.new_bytes += ref.length
                stats.stored_bytes += stored
        m = Manifest(size=done, sha256=full.hexdigest(), chunker=ck.params(), chunks=tuple(refs))
        return self.put_manifest(m), stats

    def build_manifest(self, f: BinaryIO, chunker: dict) -> Manifest:
        """保存せずにマニフェストを組み立てる(変更検出用)。"""
        ck = make_chunker(chunker)
        full = hashlib.sha256()
        refs: list[ChunkRef] = []
        h = None
        n = 0
        size = 0
        for piece, end in ck.split(f):
            full.update(piece)
            if h is None:
                h = hashlib.sha256()
            h.update(piece)
            n += len(piece)
            size += len(piece)
            if end:
                refs.append(ChunkRef(h.hexdigest(), n))
                h, n = None, 0
        return Manifest(size=size, sha256=full.hexdigest(), chunker=ck.params(), chunks=tuple(refs))

    def hash_file(self, f: BinaryIO, chunker: dict) -> str:
        """保存せずにマニフェストの sha を計算する(変更検出用)。"""
        return manifest_sha(self.build_manifest(f, chunker))

    def write_file(
        self,
        sha: str,
        out: BinaryIO,
        progress: ProgressFn | None = None,
        path: str | None = None,
    ) -> None:
        """マニフェストの内容を out に逐次書き出し、全体の SHA-256 を照合する(メモリ一定)。

        異常なら CorruptData(書き出した内容は使わないこと)。
        """
        m = self.get_manifest(sha)
        h = hashlib.sha256()
        total = 0
        for ref in m.chunks:
            for piece in self.open_chunk(ref.sha, ref.length):
                out.write(piece)
                h.update(piece)
                total += len(piece)
                if progress is not None:
                    progress(ProgressEvent("write", total, m.size, path))
        if total != m.size or h.hexdigest() != m.sha256:
            self.quarantine("manifest", sha, "content_mismatch")
            raise CorruptData(
                f"マニフェストの内容がチャンクと一致しません: {sha}", sha=sha, reason="content_mismatch"
            )

    # --- 全件検証 ---

    def verify_all(
        self, quick: bool = False, progress: ProgressFn | None = None
    ) -> StoreVerifyResult:
        """保存されている全チャンク・全マニフェストを検証する(verify の土台)。

        quick=False: 全チャンクを復号してハッシュを照合する(並列)。
        quick=True : チャンクはヘッダの確認だけ(raw なら長さも確認する)。
        マニフェストは、参照するチャンクが揃っていて長さが合うかを確認する。
        壊れたチャンク・マニフェストは隔離する。
        """
        res = StoreVerifyResult()
        chunks = list(self.iter_chunks())
        lengths: dict[str, int | None] = {}  # 健全なチャンク → 長さ(不明なら None)

        def check_one(sha: str) -> tuple[str, bool, int | None]:
            if quick:
                return (sha, *self._chunk_quick(sha))
            try:
                n = 0
                for piece in self._open_verified(sha, None):
                    n += len(piece)
                return sha, True, n
            except CorruptData:
                return sha, False, None

        for i, (sha, ok, n) in enumerate(self._pool().map(check_one, chunks), 1):
            if ok:
                lengths[sha] = n
            else:
                res.bad_chunks.append(sha)
            if progress is not None:
                progress(ProgressEvent("verify_chunks", i, len(chunks)))
        res.checked_chunks = len(chunks)

        manifests = list(self.iter_manifests())
        for i, msha in enumerate(manifests, 1):
            res.checked_manifests += 1
            try:
                m = self.get_manifest(msha)
            except CorruptData:
                res.bad_manifests.append(msha)
                continue
            for ref in m.chunks:
                if ref.sha not in lengths:
                    res.broken_manifests[msha] = f"チャンクが欠損・破損: {ref.sha}"
                    break
                n = lengths[ref.sha]
                if n is not None and n != ref.length:
                    res.broken_manifests[msha] = f"チャンクの長さが一致しない: {ref.sha}"
                    break
            if progress is not None:
                progress(ProgressEvent("verify_manifests", i, len(manifests)))
        return res

    def _chunk_quick(self, sha: str) -> tuple[bool, int | None]:
        if not self.has_chunk(sha, "exists"):
            return False, None
        path = fsutil.os_path(self.chunk_path(sha))
        try:
            with open(path, "rb") as f:
                head = f.read(1)
                size = os.fstat(f.fileno()).st_size
        except FileNotFoundError:
            return False, None
        return True, (size - 1 if head == b"\x00" else None)


def _file_size(f: BinaryIO) -> int | None:
    try:
        return os.fstat(f.fileno()).st_size
    except (AttributeError, OSError, ValueError):
        return None
