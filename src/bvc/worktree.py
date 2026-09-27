# 追跡ファイルの走査・stat キャッシュ・変更検出・復元トランザクション。設計書 1.1節・3.6節。

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
import time
import unicodedata
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any, Final

from .errors import (
    BrokenVersion,
    BvcError,
    CorruptData,
    DiskFull,
    FileBusy,
    FileChanging,
    SafetyAbort,
    UnsafePath,
    UsageError,
)
from .fsutil import (
    BVC_DIR,
    IS_WINDOWS,
    atomic_write_json,
    check_id,
    check_relpath,
    check_sha,
    compile_glob,
    fault,
    fsync_dir,
    is_link_or_reparse,
    load_json,
    makedirs,
    new_tmp_path,
    os_path,
    remove_quietly,
    replace,
    resolve_in_workdir,
)
from .model import (
    Config,
    Head,
    Manifest,
    ProgressEvent,
    PutStats,
    RestoreResult,
    WorkState,
)
from .store import ObjectStore, ProgressFn, manifest_sha

logger = logging.getLogger(__name__)

# 書き込み中のファイルの扱い(仕様書 2.5節): 読み取りの前後で stat が変われば、
# RETRY_WAIT 秒おきに読み直し、RETRY_ATTEMPTS 回とも変われば FileChanging。
# テストで待ち時間を短くできるよう、モジュール変数にしている。
# RETRY_ATTEMPTS/RETRY_WAIT はテストから差し替える可変値のため Final を付けない(大文字だが定数ではない)。
# FIXME: 定数ではないので、値の渡し方を変える
RETRY_ATTEMPTS = 3
RETRY_WAIT = 1.0

# stat キャッシュを信用する条件の余裕(FAT の更新日時の粒度。設計書 4.2節、I-14)
FS_TIME_MARGIN_NS: Final[int] = 2_000_000_000

# 復元前に確保する空き容量の余裕分(設計書 4.6節の0)
DISK_MARGIN: Final[int] = 64 << 20

# journal.json の状態(設計書 4.6節。取りうる値の集合なので可変長)
JOURNAL_STATES: Final[tuple[str, ...]] = ("staging", "swapping", "swapped")


class _ManifestCache:
    # パス → マニフェスト(得られなければ None)を1回だけ求めて覚えておく。名前変更の検知用。

    def __init__(self, load: Callable[[str], Manifest | None]):
        self._load = load
        self._cache: dict[str, Manifest | None] = {}

    def get(self, rel: str) -> Manifest | None:
        if rel not in self._cache:
            self._cache[rel] = self._load(rel)
        return self._cache[rel]


class Worktree:
    # 作業フォルダの追跡ファイルを管理する。bvc_dir は .bvc フォルダ。

    def __init__(
        self, workdir: Path, bvc_dir: Path, config: Config, store: ObjectStore
    ):
        self.workdir = Path(workdir)
        self._bvc_dir = Path(bvc_dir)
        self._tmp = self._bvc_dir / "tmp"
        self._index_file = self._bvc_dir / "index.json"
        # 復元トランザクションの作業域と記録(設計書 4.6節)
        self._txn = self._bvc_dir / "txn"
        self._journal_file = self._bvc_dir / "journal.json"
        self.config = config
        self.store = store
        self._track = [compile_glob(p) for p in config.track]
        self._ignore = [compile_glob(p) for p in config.ignore]
        self._rules = [
            (
                compile_glob(r["pattern"]),
                r.get("chunker", config.chunker),
                r.get("compression"),
            )
            for r in config.rules
        ]
        # 直前の走査で見つけた、正規化後のパス → 実際の名前('/' 区切り)
        self._names: dict[str, str] = {}
        # 直前の state で確かめた stat(size, mtime_ns)。index の記録に使う
        self._stats: dict[str, tuple[int, int]] = {}

    # --- 走査(M2-7、設計書 4.1節、仕様書 2.4節・2.9節) ---

    def is_tracked(self, rel: str) -> bool:
        return any(p.fullmatch(rel) for p in self._track) and not any(
            p.fullmatch(rel) for p in self._ignore
        )

    def scan(self) -> list[str]:
        # 追跡対象の相対パス('/' 区切り、NFC)を列挙する。
        return sorted(self._scan())

    def _scan(self) -> dict[str, str]:
        # リンク・リパースポイントはたどらずに対象外とし、パターンに一致すれば警告する。
        # 大文字小文字だけ(または正規化の前後だけ)が異なるパスが両方追跡対象ならエラー。
        found: dict[str, str] = {}
        seen: dict[str, str] = {}
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            dir_path = self.workdir / rel_dir if rel_dir else self.workdir
            try:
                with os.scandir(os_path(dir_path)) as it:
                    entries = list(it)
            except OSError as e:
                raise FileBusy(
                    f"フォルダを読み取れません: {rel_dir or '.'}({e})", path=rel_dir
                ) from e
            for e in entries:
                if not rel_dir and e.name.casefold() == BVC_DIR.casefold():
                    continue
                raw = f"{rel_dir}/{e.name}" if rel_dir else e.name
                try:
                    st = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if is_link_or_reparse(st):
                    if self._matches_quietly(raw):
                        logger.warning("リンクは追跡しません: %s", raw)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(raw)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                try:
                    rel = check_relpath(raw)
                except UnsafePath:
                    if self._matches_quietly(raw):
                        logger.warning("記録できない名前のため追跡しません: %s", raw)
                    continue
                if not self.is_tracked(rel):
                    continue
                key = rel.casefold()
                if key in seen:
                    raise UnsafePath(
                        f"大文字小文字などだけが異なるパスが両方とも追跡対象です: {seen[key]} と {rel}",
                        path=rel,
                    )
                seen[key] = rel
                found[rel] = raw
        self._names = found
        return found

    def _matches_quietly(self, raw: str) -> bool:
        try:
            return self.is_tracked(raw.replace("\\", "/"))
        except Exception:  # noqa: BLE001
            return False

    def _rule_for(self, rel: str) -> tuple[dict, str | None]:
        # rules を上から照合し、最初に一致したものの (chunker, compression) を返す。
        for pattern, chunker, compression in self._rules:
            if pattern.fullmatch(rel):
                return chunker, compression
        return self.config.chunker, None

    # --- 変更検出(M2-8, M2-9、設計書 4.2節) ---

    def state(
        self,
        base_tree: dict[str, str],
        store_chunks: bool,
        no_cache: Collection[str] = (),
        renames: Collection[tuple[str, str]] = (),
        find_hints: bool = True,
        progress: ProgressFn | None = None,
    ) -> WorkState:
        # 作業フォルダの状態を base_tree と比べる。store_chunks なら変わったファイルを保存する。
        # progress は保存するファイルの進捗(ProgressEvent("put", ...))の通知先。
        # renames は名前変更の手動指定(正規化済みの相対パスの組)。当てはまらなければ UsageError。
        # find_hints なら、missing についてパターン外の同じ内容のファイルを探す(読み込みを伴う)。
        fs_time_ns = self._mark_fs_time()
        index, index_fs_time = self._load_index()
        files = self._scan()

        tree: dict[str, str] = {}
        stats: dict[str, tuple[int, int]] = {}
        total_bytes = 0
        new_bytes = 0
        for rel in sorted(files):
            full = self.workdir / files[rel]
            try:
                st = os.stat(os_path(full))
            except FileNotFoundError:
                continue  # 走査の後に消えた。欠落として扱う
            except OSError as e:
                raise FileBusy(
                    f"ファイルにアクセスできません: {rel}({e})", path=rel
                ) from e

            ent = index.get(rel)
            if (
                ent is not None
                and rel not in no_cache
                and ent["size"] == st.st_size
                and ent["mtime_ns"] == st.st_mtime_ns
                and ent["mtime_ns"] < index_fs_time - FS_TIME_MARGIN_NS
                and (
                    # 保存するときは、再利用するマニフェストとチャンクが健全か確かめる。
                    # 壊れていれば読み直して保存し直す(設計書 4.11節、C-1)
                    not store_chunks
                    or self.store.manifest_ok(
                        ent["manifest"], self.config.commit_verify
                    )
                )
            ):
                tree[rel] = ent["manifest"]
                stats[rel] = (st.st_size, st.st_mtime_ns)
                total_bytes += st.st_size
                continue

            sha, put, st2 = self._hash_file(full, rel, store_chunks, progress)
            tree[rel] = sha
            stats[rel] = (st2.st_size, st2.st_mtime_ns)
            total_bytes += st2.st_size
            new_bytes += put.new_bytes

        modified = sorted(p for p in tree if p in base_tree and base_tree[p] != tree[p])
        added = sorted(p for p in tree if p not in base_tree)
        gone = sorted(p for p in base_tree if p not in tree)

        # 名前変更の検知(設計書 4.3節)
        renamed, missing, hints = self._detect_renames(
            gone,
            added,
            base_tree,
            tree,
            renames,
            self.config.rename_threshold,
            find_hints,
        )
        renamed_to = {r[1] for r in renamed}
        added = [p for p in added if p not in renamed_to]

        self._stats = stats
        return WorkState(
            tree=tree,
            modified=modified,
            added=added,
            renamed=renamed,
            missing=missing,
            hints=hints,
            total_bytes=total_bytes,
            new_bytes=new_bytes,
            fs_time_ns=fs_time_ns,
        )

    def _hash_file(
        self,
        full: Path,
        rel: str,
        store_chunks: bool,
        progress: ProgressFn | None = None,
    ) -> tuple[str, PutStats, os.stat_result]:
        # stat₁ → 読み込み(分割・ハッシュ・保存)→ stat₂。stat が変わっていれば読み直す。
        chunker, compression = self._rule_for(rel)
        p = os_path(full)
        for attempt in range(RETRY_ATTEMPTS):
            try:
                st1 = os.stat(p)
                f = open(p, "rb")  # noqa: SIM115
            except FileNotFoundError as e:
                raise FileChanging(
                    f"読み取り中にファイルが消えました: {rel}", path=rel
                ) from e
            except OSError as e:
                raise FileBusy(
                    f"ファイルを開けません(他のアプリが使用中の可能性があります): {rel}({e})",
                    path=rel,
                ) from e
            with f:
                try:
                    if store_chunks:
                        sha, put = self.store.put_file(
                            f,
                            chunker,
                            self.config.commit_verify,
                            progress=progress,
                            path=rel,
                            compression=compression,
                        )
                    else:
                        sha = self.store.hash_file(f, chunker)
                        put = PutStats(size=st1.st_size)
                except OSError as e:
                    raise FileBusy(
                        f"ファイルの読み取り・保存に失敗しました: {rel}({e})", path=rel
                    ) from e
            try:
                st2 = os.stat(p)
            except FileNotFoundError as e:
                raise FileChanging(
                    f"読み取り中にファイルが消えました: {rel}", path=rel
                ) from e
            if (st1.st_size, st1.st_mtime_ns) == (
                st2.st_size,
                st2.st_mtime_ns,
            ) and put.size == st2.st_size:
                return sha, put, st2
            if attempt + 1 < RETRY_ATTEMPTS:
                logger.info("書き込み中のため読み直します: %s", rel)
                time.sleep(RETRY_WAIT)
        raise FileChanging(
            f"ファイルが書き込み中です(読み取りの前後で変わりました): {rel}", path=rel
        )

    # --- stat キャッシュ(M2-8、設計書 2.5節) ---

    def update_index(self, tree: dict[str, str], fs_time_ns: int) -> None:
        # tree の各ファイルの stat を記録する。fs_time_ns は tree を得た走査の開始時点の時刻。
        # 直前の state で確かめた stat があればそれを使う(読み取り後に変わった場合も、
        # 更新日時が fs_time_ns 以降になるので、次の走査で必ずハッシュし直される)。
        entries = {}
        for rel, sha in tree.items():
            st = self._stats.get(rel)
            if st is None:
                try:
                    s = os.stat(os_path(self.workdir / self._names.get(rel, rel)))
                except FileNotFoundError:
                    continue
                st = (s.st_size, s.st_mtime_ns)
            entries[rel] = {"size": st[0], "mtime_ns": st[1], "manifest": sha}
        atomic_write_json(
            self._index_file,
            {"format": 1, "fs_time_ns": fs_time_ns, "entries": entries},
            self._tmp,
        )

    def check_index(self) -> str | None:
        # index.json の異常("missing" / 理由の文字列)を返す。正常なら None(管理ファイルの自動復旧用)。
        # 知らない format(UnsupportedFormat)と読み込みの OSError はそのまま送出する(D-15)。
        if not os.path.lexists(os_path(self._index_file)):
            return "missing"
        try:
            self._read_index()
        except CorruptData as e:
            return str(e)
        return None

    def reset_index(self) -> None:
        # index.json を空で作り直す。次の走査で全追跡ファイルをハッシュし直す(設計書 4.12節)。
        atomic_write_json(
            self._index_file, {"format": 1, "fs_time_ns": 0, "entries": {}}, self._tmp
        )

    def _load_index(self) -> tuple[dict[str, dict], int]:
        # (entries, fs_time_ns) を返す。無い・壊れていれば空(作り直す)。
        # 知らない format(UnsupportedFormat)と読み込みの OSError はそのまま送出する(D-15)。
        try:
            return self._read_index()
        except FileNotFoundError:
            return {}, 0
        except CorruptData as e:
            logger.warning("%s(作り直します)", e)
            return {}, 0

    def _read_index(self) -> tuple[dict[str, dict], int]:
        # index.json を読んで検査する。無ければ FileNotFoundError、壊れていれば CorruptData。
        data = load_json(self._index_file, "index.json")
        fs_time_ns = data.get("fs_time_ns")
        entries = data.get("entries")
        out: dict[str, dict] = {}
        ok = type(fs_time_ns) is int and isinstance(entries, dict)
        if ok:
            for rel, ent in entries.items():
                try:
                    if not (
                        isinstance(ent, dict)
                        and type(ent.get("size")) is int
                        and type(ent.get("mtime_ns")) is int
                    ):
                        raise UnsafePath("")
                    check_sha(ent.get("manifest"))
                    out[check_relpath(rel)] = ent
                except UnsafePath:
                    ok = False
                    break
        if not ok:
            raise CorruptData("index.json: 内容が不正です")
        return out, fs_time_ns

    # --- 名前変更の検知(M4-5, M4-6、設計書 4.3節、仕様書 2.6節) ---

    def _detect_renames(
        self,
        gone: list[str],
        added: list[str],
        base_tree: dict[str, str],
        tree: dict[str, str],
        renames: Collection[tuple[str, str]],
        threshold: float,
        find_hints: bool,
    ) -> tuple[list[tuple[str, str, float]], list[str], dict[str, list[str]]]:
        # 消えた集合 G と新しい集合 N を対応付け、(renamed, missing, hints) を返す。
        # 結果は入力だけで決まる(集合の反復順に依存しない)ようにする。
        g_left = set(gone)
        n_left = set(added)
        renamed: list[tuple[str, str, float]] = []

        def take(g: str, n: str, sim: float) -> None:
            renamed.append((g, n, sim))
            g_left.discard(g)
            n_left.discard(n)

        # 1. 手動指定(--rename)を確定する。当てはまらない指定は、推測せずに中止する
        for g, n in self._resolve_manual_renames(renames, g_left, n_left):
            take(g, n, 1.0)

        old_m = _ManifestCache(lambda g: self._old_manifest(base_tree[g]))
        new_m = _ManifestCache(lambda n: self._new_manifest(n, tree[n]))

        # 2. 内容(manifest.sha256)が一致する組を確定する(1対1。複数あればパスの辞書順)。
        # マニフェストの名前が同じなら内容も同じなので、まずそれで判定する(読み込み不要)。
        # 残りは分割方式が異なる場合に備えて、内容全体のハッシュで比べる
        for g in sorted(g_left):
            n = next((n for n in sorted(n_left) if tree[n] == base_tree[g]), None)
            if n is not None:
                take(g, n, 1.0)
        if g_left and n_left:
            by_content: dict[str, list[str]] = {}
            for n in sorted(n_left):
                m = new_m.get(n)
                if m is not None:
                    by_content.setdefault(m.sha256, []).append(n)
            for g in sorted(g_left):
                m = old_m.get(g)
                cands = by_content.get(m.sha256) if m is not None else None
                if cands:
                    take(g, cands.pop(0), 1.0)

        # 3. 残りについて sim(g, n) = |chunks(g) ∩ chunks(n)| のバイト数 / size(n) を計算し、
        # sim ≥ threshold の組を sim の高い順に貪欲に確定する(同じ sim ならパスの辞書順)
        if g_left and n_left:
            sims: list[tuple[float, str, str]] = []
            for g in sorted(g_left):
                gm = old_m.get(g)
                if gm is None:
                    continue
                g_chunks = {c.sha for c in gm.chunks}
                for n in sorted(n_left):
                    nm = new_m.get(n)
                    if nm is None or nm.size == 0:
                        continue
                    common = sum(c.length for c in nm.chunks if c.sha in g_chunks)
                    sim = common / nm.size
                    if sim >= threshold:
                        sims.append((sim, g, n))
            sims.sort(key=lambda x: (-x[0], x[1], x[2]))
            for sim, g, n in sims:
                if g in g_left and n in n_left:
                    take(g, n, sim)

        # 4. 残った G を missing とする
        missing = sorted(g_left)

        # 5. missing ごとに、パターン外で同じ内容のファイルを探す(ヒント)
        hints = self._find_hints(missing, old_m) if find_hints and missing else {}
        return renamed, missing, hints

    def _resolve_manual_renames(
        self,
        renames: Collection[tuple[str, str]],
        gone: set[str],
        added: set[str],
    ) -> list[tuple[str, str]]:
        # 手動指定の組を、消えたパス・新しいパスに対応付ける。
        # 大文字小文字だけが異なる指定も受け付ける(一意に決まる場合のみ)。
        def resolve(p: str, pool: set[str], what: str) -> str:
            if p in pool:
                return p
            found = [q for q in pool if q.casefold() == p.casefold()]
            if len(found) == 1:
                return found[0]
            raise UsageError(f"--rename: {p} は{what}ではありません", path=p)

        out: list[tuple[str, str]] = []
        used_g: set[str] = set()
        used_n: set[str] = set()
        for g, n in renames:
            g = resolve(g, gone, "前の版にあって作業フォルダから消えた追跡ファイル")
            n = resolve(n, added, "新しく追加された追跡ファイル")
            if g in used_g or n in used_n:
                raise UsageError(
                    f"--rename: 同じパスが複数回指定されています: {g}={n}", path=g
                )
            used_g.add(g)
            used_n.add(n)
            out.append((g, n))
        return out

    def _old_manifest(self, sha: str) -> Manifest | None:
        # 前の版のマニフェスト。壊れていれば None(対応付けに使わない。その版は verify で扱う)。
        try:
            return self.store.get_manifest(sha)
        except CorruptData:
            return None

    def _new_manifest(self, rel: str, sha: str) -> Manifest | None:
        # 作業ファイルのマニフェスト。保存済みならそれを読み、無ければ(保存しない status のとき)
        # ファイルから組み立て直す。保存されていないマニフェストを get_manifest で読むと
        # 「欠落」として隔離記録されてしまうため、先に存在を確かめる。
        if os.path.exists(os_path(self.store.manifest_path(sha))):
            try:
                return self.store.get_manifest(sha)
            except CorruptData:
                pass
        chunker, _ = self._rule_for(rel)
        full = self.workdir / self._names.get(rel, rel)
        try:
            with open(os_path(full), "rb") as f:
                m = self.store.build_manifest(f, chunker)
        except FileNotFoundError as e:
            raise FileChanging(
                f"読み取り中にファイルが消えました: {rel}", path=rel
            ) from e
        except OSError as e:
            raise FileBusy(f"ファイルを読み取れません: {rel}({e})", path=rel) from e
        # 走査の後に書き換わっていれば、この走査の結果とは別物なので使わない
        return m if manifest_sha(m) == sha else None

    def _find_hints(
        self, missing: list[str], old_m: _ManifestCache
    ) -> dict[str, list[str]]:
        # 作業フォルダ直下と、missing のあったフォルダだけを探す(設計書 4.3節の5)。
        # 追跡パターン外の通常ファイルのうち、サイズと内容全体の SHA-256 が一致するもの。
        # 表示のためだけの情報なので、読めないファイルは飛ばす。
        tracked = {p.casefold() for p in self._names}
        listed: dict[str, list[tuple[str, int]]] = {}
        hints: dict[str, list[str]] = {}
        for g in missing:
            m = old_m.get(g)
            if m is None:
                continue
            folder = g.rpartition("/")[0]
            found: list[str] = []
            for d in dict.fromkeys(["", folder]):
                if d not in listed:
                    listed[d] = self._list_untracked(d, tracked)
                for rel, size in listed[d]:
                    if size == m.size and self._content_sha256(rel) == m.sha256:
                        found.append(rel)
            if found:
                hints[g] = found
        return hints

    def _list_untracked(self, rel_dir: str, tracked: set[str]) -> list[tuple[str, int]]:
        # rel_dir 直下の、追跡対象でない通常ファイルの (相対パス, サイズ)。リンクは含めない。
        dir_path = self.workdir / rel_dir if rel_dir else self.workdir
        out: list[tuple[str, int]] = []
        try:
            with os.scandir(os_path(dir_path)) as it:
                entries = list(it)
        except OSError:
            return out
        for e in entries:
            if not rel_dir and e.name.casefold() == BVC_DIR.casefold():
                continue
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if unicodedata.normalize("NFC", rel).casefold() in tracked:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
                continue
            out.append((rel, st.st_size))
        return sorted(out)

    def _content_sha256(self, rel: str) -> str | None:
        try:
            with open(os_path(self.workdir / rel), "rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest()
        except OSError:
            return None

    # --- 修復の候補(verify --repair。設計書 4.9節) ---

    def repair_candidates(
        self, names: Collection[str], sizes: Collection[int]
    ) -> list[str]:
        # 修復の材料にするファイル(作業フォルダからの相対パス。実際の名前)を返す。
        # 追跡ファイルすべてと、追跡対象外の通常ファイルのうち、ファイル名(大文字小文字を問わない)が
        # names のどれかと一致するもの、またはサイズが sizes のどれかと一致するもの。
        # リンクはたどらず、.bvc と読めないフォルダは飛ばす。
        tracked = self._scan()
        untracked: list[str] = []
        seen = {rel.casefold() for rel in tracked}
        names_fold = {n.casefold() for n in names}
        size_set = set(sizes)
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            dir_path = self.workdir / rel_dir if rel_dir else self.workdir
            try:
                with os.scandir(os_path(dir_path)) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if not rel_dir and e.name.casefold() == BVC_DIR.casefold():
                    continue
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if is_link_or_reparse(st):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(rel)
                elif (
                    stat.S_ISREG(st.st_mode)
                    and unicodedata.normalize("NFC", rel).casefold() not in seen
                    and (e.name.casefold() in names_fold or st.st_size in size_set)
                ):
                    untracked.append(rel)
        return [tracked[rel] for rel in sorted(tracked)] + sorted(untracked)

    def _mark_fs_time(self) -> int:
        # .bvc/tmp に空の目印ファイルを作り、その更新日時を読んで消す(設計書 2.5節、I-14)。
        mark = new_tmp_path(self._tmp)
        try:
            with open(os_path(mark), "wb"):
                pass
            return os.stat(os_path(mark)).st_mtime_ns
        finally:
            remove_quietly(mark)

    # --- 復元の事前検査(M3-3、設計書 4.1節・4.6節の0) ---

    def check_target(self, target_tree: dict[str, str]) -> list[str]:
        # 移動先の tree を検査する(何も変えない)。走査した追跡ファイルの一覧を返す。
        # パスの検査、全マニフェストの健全性(壊れていれば BrokenVersion)、
        # 追跡対象外のファイル・フォルダ・リンクとの衝突(SafetyAbort)を確かめる。
        tracked = self._scan()
        self._check_target(target_tree, tracked)
        return sorted(tracked)

    def _check_target(
        self, target_tree: dict[str, str], tracked: dict[str, str]
    ) -> None:
        # tracked は追跡ファイルの 正規化後のパス → 実際の名前。
        folded: dict[str, str] = {}
        for rel in sorted(target_tree):
            if check_relpath(rel) != rel:
                raise UnsafePath(
                    f"移動先の版に正規化されていないパスがあります: {rel!r}", path=rel
                )
            check_sha(target_tree[rel])
            key = rel.casefold()
            if IS_WINDOWS and key in folded:
                raise UnsafePath(
                    f"移動先の版に、大文字小文字だけが異なるパスがあります: {folded[key]} と {rel}",
                    path=rel,
                )
            folded[key] = rel
        for sha in sorted(set(target_tree.values())):
            if not self.store.manifest_ok(sha, "exists"):
                raise BrokenVersion(
                    f"移動先の版の保存データが壊れているか欠けています(マニフェスト {sha})",
                    sha=sha,
                )
        tracked_fold = {rel.casefold(): rel for rel in tracked}
        deleting = {rel for rel in tracked if rel not in target_tree}
        for rel in sorted(target_tree):
            self._check_collision(rel, tracked, tracked_fold, deleting)

    def _check_collision(
        self,
        rel: str,
        tracked: dict[str, str],
        tracked_fold: dict[str, str],
        deleting: set[str],
    ) -> None:
        # 書き出し先 rel(とその途中のフォルダ)に、追跡対象外のものが無いか確かめる(仕様書 2.8節、R-10)。
        # 追跡ファイルの場所なら衝突ではない(置き換えるか、先に削除する)。
        parts = rel.split("/")
        current = self.workdir
        for i, part in enumerate(parts):
            current = current / part
            sub = "/".join(parts[: i + 1])
            try:
                st = os.lstat(os_path(current))
            except FileNotFoundError:
                break  # ここから先は無い(復元時に作る)
            last = i == len(parts) - 1
            if not last and stat.S_ISDIR(st.st_mode) and not is_link_or_reparse(st):
                continue
            owner = self._tracked_owner(sub, st, tracked, tracked_fold)
            if owner is not None and owner in deleting:
                return  # 先に削除する追跡ファイル(書き出しの直前に、改めてパスを検査する)
            if owner is not None and last and owner == rel:
                break
            what = "フォルダ" if stat.S_ISDIR(st.st_mode) else "ファイル"
            if is_link_or_reparse(st):
                what = "リンク"
            raise SafetyAbort(
                f"書き出し先に追跡対象外の{what}があります: {sub}\n"
                "  保存されていない内容を上書きしないよう中止しました。移動するか削除してから、やり直してください",
                path=sub,
            )
        resolve_in_workdir(self.workdir, rel)

    def _tracked_owner(
        self,
        sub: str,
        st: os.stat_result,
        tracked: dict[str, str],
        tracked_fold: dict[str, str],
    ) -> str | None:
        # sub にあるもの(lstat の結果 st)が追跡ファイルなら、その正規化後のパスを返す。
        # 大文字小文字を区別しないファイルシステムでは、別の綴りの追跡ファイルと同じ実体のことがある。
        if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
            return None
        for cand in dict.fromkeys([sub, tracked_fold.get(sub.casefold())]):
            if cand is None or cand not in tracked:
                continue
            try:
                cst = os.lstat(os_path(self.workdir / tracked[cand]))
            except FileNotFoundError:
                continue
            if os.path.samestat(cst, st):
                return cand
        return None

    # --- 復元トランザクション(M3-2, M3-4、設計書 4.6節) ---

    def restore(
        self,
        target_tree: dict[str, str],
        current: WorkState,
        head: Head,
        on_committed: Callable[[Head], None],
        progress: ProgressFn | None = None,
    ) -> RestoreResult:
        # 作業フォルダの追跡ファイルを target_tree に一致させる。全部成功か全部元通りのどちらか。
        # current は直前の state の結果(その時点の内容と stat を、退避の直前に再確認する)。
        # 置き換えが済んだら on_committed(head) で HEAD を更新する。
        if os.path.lexists(os_path(self._journal_file)):
            raise BvcError(
                "中断した復元の記録(journal.json)が残っています。bvc を実行し直してください"
            )
        tracked = {rel: self._names.get(rel, rel) for rel in current.tree}
        self._check_target(target_tree, tracked)
        ops = self._plan(target_tree, current, tracked)
        result = RestoreResult(
            written=[op.path for op in ops if op.kind == "write"],
            deleted=[op.path for op in ops if op.kind == "delete"],
        )
        journal = {
            "format": 1,
            "state": "staging",
            "head": {"at": head.at, "branch": head.branch},
            "fs_time_ns": current.fs_time_ns,
            "target": dict(target_tree),
            "ops": [op.to_json() for op in ops],
            "done": 0,
        }
        if not ops:
            self._complete(journal, ops, on_committed, has_journal=False)
            return result

        self._check_space(ops)
        self._prepare_txn()
        try:
            self._write_journal(journal)
            self._stage(ops, progress)
        except BaseException as e:
            self._discard_txn()
            if isinstance(e, CorruptData):
                raise BrokenVersion(
                    f"移動先の版の保存データが壊れています({e})", **e.details
                ) from e
            if isinstance(e, OSError) and e.errno == errno.ENOSPC:
                raise DiskFull(f"空き容量が足りないため中止しました({e})") from e
            if isinstance(e, OSError):
                raise FileBusy(
                    f"復元の準備中に読み書きに失敗したため中止しました(作業ファイルは変わっていません): {e}"
                ) from e
            raise

        try:
            journal["state"] = "swapping"
            journal["ops"] = [op.to_json() for op in ops]
            self._write_journal(journal)
            self._swap(ops, journal, progress)
            journal["state"] = "swapped"
            self._write_journal(journal)
        except BaseException as e:
            try:
                self._rollback(ops)
                self._abandon(journal)
            except BaseException as e2:  # noqa: BLE001
                raise FileBusy(
                    f"復元に失敗し、元に戻す処理も完了できませんでした({e2})。"
                    "次に bvc を実行したときに、もう一度元に戻します",
                ) from e
            if isinstance(e, OSError):
                raise FileBusy(
                    f"作業ファイルを置き換えられないため、すべて元に戻して中止しました(使用中の可能性があります): {e}"
                ) from e
            raise
        self._complete(journal, ops, on_committed, has_journal=True)
        return result

    def recover(self, on_committed: Callable[[Head], None]) -> None:
        # 中断した復元の後始末(M3-5、設計書 4.6節の表)。何度実行しても同じ結果になる。
        # staging: 作業域を消す。swapping: すべて元に戻す。swapped: on_committed から完了させる。
        if not os.path.lexists(os_path(self._journal_file)):
            self._clean_leftovers()
            return
        journal, ops = self._read_journal()
        state = journal["state"]
        if state == "staging":
            if self._list_dir(self._txn / "old"):
                raise BvcError(
                    f"中断した復元の記録と作業域の内容が一致しません。{self._txn / 'old'} を確認してください"
                )
            self._discard_txn()
            logger.warning(
                "中断していた復元を取り消しました(作業ファイルは変わっていません)"
            )
        elif state == "swapping":
            self._rollback(ops)
            self._abandon(journal)
            logger.warning(
                "中断していた復元を元に戻しました(作業ファイルは復元前の状態です)"
            )
        else:
            self._stats = {}
            self._names = {}
            self._complete(journal, ops, on_committed, has_journal=True)
            logger.warning(
                "中断していた復元を完了しました(版 %d)", journal["head"]["at"]
            )

    def _plan(
        self, target_tree: dict[str, str], current: WorkState, tracked: dict[str, str]
    ) -> list[_Op]:
        # 削除を先に、書き出しを後にする(大文字小文字だけの名前変更で、同じ実体を先に退避するため)。
        ops: list[_Op] = []
        for rel in sorted(current.tree):
            if rel not in target_tree:
                ops.append(
                    _Op(len(ops), "delete", rel, tracked[rel], None, self._stats[rel])
                )
        for rel in sorted(target_tree):
            if current.tree.get(rel) != target_tree[rel]:
                src = tracked[rel] if rel in current.tree else None
                expect = self._stats[rel] if src is not None else None
                ops.append(_Op(len(ops), "write", rel, src, target_tree[rel], expect))
        return ops

    def _check_space(self, ops: list[_Op]) -> None:
        # 書き出す内容の合計 + 余裕分の空き容量があるか(R-6)。
        need = sum(
            self.store.get_manifest(op.sha).size for op in ops if op.kind == "write"
        )
        makedirs(self._txn)
        free = shutil.disk_usage(os_path(self._txn)).free
        if free < need + DISK_MARGIN:
            raise DiskFull(
                f"空き容量が足りません(必要: {need + DISK_MARGIN} バイト、空き: {free} バイト)",
                need=need + DISK_MARGIN,
                free=free,
            )

    def _stage(self, ops: list[_Op], progress: ProgressFn | None) -> None:
        # 書き出す内容を txn/new/<n> に展開する(全体 SHA-256 を照合し、fsync)。
        new_dir = self._txn / "new"
        makedirs(new_dir)
        for op in ops:
            if op.kind != "write":
                continue
            dst = new_dir / str(op.n)
            with open(os_path(dst), "xb") as f:
                self.store.write_file(op.sha, f, progress=progress, path=op.path)
                f.flush()
                os.fsync(f.fileno())
            st = os.stat(os_path(dst))
            op.staged = (st.st_size, st.st_mtime_ns)
            fault(f"stage:{op.n}")

    def _swap(self, ops: list[_Op], journal: dict, progress: ProgressFn | None) -> None:
        # 各 op を順に実行し、1件ごとに journal の done を更新する。
        new_dir, old_dir = self._txn / "new", self._txn / "old"
        makedirs(old_dir)
        for i, op in enumerate(ops):
            if op.src is not None:
                src = self._work_path(op.src)
                old = old_dir / str(op.n)
                self._recheck(src, op)
                replace(src, old, f"stash:{op.n}")
                # 確認から退避までの間の書き換えも見逃さないよう、退避した実体でもう一度確かめる
                self._recheck(old, op)
            if op.kind == "write":
                dst = resolve_in_workdir(self.workdir, op.path)
                if os.path.lexists(os_path(dst)):
                    raise FileChanging(
                        f"書き出し先に新しいファイルがあります: {op.path}", path=op.path
                    )
                makedirs(dst.parent)
                replace(new_dir / str(op.n), dst, f"swap:{op.n}")
                fsync_dir(dst.parent)
            journal["done"] = i + 1
            self._write_journal(journal)
            if progress is not None:
                progress(ProgressEvent("restore", i + 1, len(ops), op.path))

    def _recheck(self, src: Path, op: _Op) -> None:
        # 退避の直前に、state の時点から変わっていないか確かめる(設計書 4.1節、R-11)。
        try:
            st = os.lstat(os_path(src))
        except FileNotFoundError:
            raise FileChanging(
                f"復元の途中でファイルが消えました: {op.path}", path=op.path
            ) from None
        if (
            is_link_or_reparse(st)
            or not stat.S_ISREG(st.st_mode)
            or (st.st_size, st.st_mtime_ns) != op.expect
        ):
            raise FileChanging(
                f"確認の後にファイルが変更されたため、すべて元に戻して中止しました: {op.path}",
                path=op.path,
            )

    def _rollback(self, ops: list[_Op]) -> None:
        # 逆順に元へ戻す。ファイルの有無から各 op の進み具合を判断するので、何度実行してもよい。
        # 1. new/<n> が無ければ、書き出した内容を new/<n> へ戻す(置いた時の stat と一致する場合だけ)
        # 2. old/<n> があれば、退避した元のファイルを元の場所へ戻す
        new_dir, old_dir = self._txn / "new", self._txn / "old"
        for op in reversed(ops):
            new, old = new_dir / str(op.n), old_dir / str(op.n)
            if op.kind == "write" and not os.path.lexists(os_path(new)):
                dst = resolve_in_workdir(self.workdir, op.path)
                try:
                    st = os.lstat(os_path(dst))
                except FileNotFoundError:
                    st = None
                if (
                    st is None
                    or op.staged is None
                    or (st.st_size, st.st_mtime_ns) != op.staged
                ):
                    raise BvcError(
                        f"元に戻せません: {op.path} が復元の途中で変更されたか、見つかりません。"
                        f"{self._txn} と {self._journal_file} を確認してください",
                        path=op.path,
                    )
                replace(dst, new, f"unswap:{op.n}")
            if os.path.lexists(os_path(old)):
                if op.src is None:
                    raise BvcError(
                        f"中断した復元の記録と作業域の内容が一致しません: {old}"
                    )
                src = self._work_path(op.src)
                if os.path.lexists(os_path(src)):
                    raise BvcError(
                        f"元に戻せません: {op.src} に別のファイルがあります。"
                        f"元の内容は {old} にあります",
                        path=op.src,
                    )
                makedirs(src.parent)
                replace(old, src, f"unstash:{op.n}")

    def _complete(
        self,
        journal: dict,
        ops: list[_Op],
        on_committed: Callable[[Head], None],
        has_journal: bool,
    ) -> None:
        # 置き換えの完了後: HEAD の更新 → index の更新 → 作業域と journal の削除。
        # ここで失敗しても journal は swapped のまま残り、次の recover で完了させる。
        head = Head(journal["head"]["at"], journal["head"]["branch"])
        try:
            on_committed(head)
            for op in ops:
                if op.kind == "delete":
                    self._stats.pop(op.path, None)
                    self._names.pop(op.path, None)
                elif op.staged is not None:
                    self._stats[op.path] = op.staged
                    self._names[op.path] = op.path
            # fs_time_ns は復元を始める前の時刻。復元したファイルは次の走査で必ずハッシュし直される(I-19)
            self.update_index(journal["target"], journal["fs_time_ns"])
            if has_journal:
                self._discard_txn()
        except Exception as e:
            if not has_journal:
                raise
            raise BvcError(
                f"作業ファイルの復元は完了しましたが、後処理に失敗しました({e})。"
                "次に bvc を実行したときに自動で完了します"
            ) from e

    # --- 作業域と journal ---

    def _work_path(self, raw: str) -> Path:
        # 作業ファイルの実際の名前('/' 区切り)を、書き込み・削除の直前に検査してパスにする(K-4)。
        resolve_in_workdir(self.workdir, raw)
        return self.workdir.joinpath(*raw.split("/"))

    def _write_journal(self, journal: dict) -> None:
        atomic_write_json(self._journal_file, journal, self._tmp)

    def _read_journal(self) -> tuple[dict, list[_Op]]:
        # journal.json を読み、記録されたパス・ハッシュ・番号を検査する(K-3)。
        # 壊れている・不正な値がある場合は、何も変えずに中止する(D-15)。
        data = load_json(self._journal_file, "journal.json")
        try:
            if data["state"] not in JOURNAL_STATES:
                raise ValueError("state")
            head = data["head"]
            data["head"] = {
                "at": check_id(head["at"]),
                "branch": check_id(head["branch"]),
            }
            if type(data["fs_time_ns"]) is not int:
                raise ValueError("fs_time_ns")
            target = data["target"]
            if not isinstance(target, dict):
                raise ValueError("target")  # noqa: TRY004
            for rel, sha in target.items():
                if check_relpath(rel) != rel:
                    raise ValueError("target")
                check_sha(sha)
            raw_ops = data["ops"]
            if not isinstance(raw_ops, list):
                raise ValueError("ops")  # noqa: TRY004
            ops = [
                _Op.from_json(o, i, data["state"] != "staging")
                for i, o in enumerate(raw_ops)
            ]
            done = data["done"]
            if type(done) is not int or not 0 <= done <= len(ops):
                raise ValueError("done")
        except (KeyError, TypeError, ValueError, UnsafePath) as e:
            raise CorruptData(
                f"中断した復元の記録(journal.json)が壊れています({e})。"
                f"何も変更せずに中止しました。{self._journal_file} と {self._txn} を確認してください"
            ) from e
        return data, ops

    def _prepare_txn(self) -> None:
        # 作業域を空にする。old/ に残骸があれば、元のファイルの可能性があるので中止する。
        if self._list_dir(self._txn / "old"):
            raise BvcError(
                f"作業域に前回の復元の残骸があります。{self._txn / 'old'} を確認してください"
            )
        self._discard_dir(self._txn / "new")

    def _abandon(self, journal: dict) -> None:
        # 元に戻し終えた後の後始末。先に journal を staging(作業ファイルは未着手)に戻してから作業域を消す。
        # 作業域を消している途中で中断しても、次の recover が new/ の無い swapping を元に戻そうとしないように。
        journal["state"] = "staging"
        journal["done"] = 0
        self._write_journal(journal)
        self._discard_txn()

    def _discard_txn(self) -> None:
        # 作業域(txn/new, txn/old)を消してから journal を消す。
        fault("txn_cleanup")
        self._discard_dir(self._txn / "old")
        self._discard_dir(self._txn / "new")
        fault("remove:journal.json")
        remove_quietly(self._journal_file)
        fsync_dir(self._bvc_dir)

    def _clean_leftovers(self) -> None:
        # journal が無いときの作業域の残骸。new/ は不要なデータなので消す。
        # old/ は元のファイルの可能性があるので消さずに警告する。
        self._discard_dir(self._txn / "new")
        if self._list_dir(self._txn / "old"):
            logger.warning(
                "作業域に前回の復元の残骸があります。%s を確認してください",
                self._txn / "old",
            )

    def _list_dir(self, d: Path) -> list[str]:
        try:
            with os.scandir(os_path(d)) as it:
                return [e.name for e in it]
        except FileNotFoundError:
            return []

    def _discard_dir(self, d: Path) -> None:
        for name in self._list_dir(d):
            _force_remove(d / name)
        try:
            os.rmdir(os_path(d))
        except FileNotFoundError:
            pass


class _Op:
    # 復元の1操作。kind は "write"(path に sha の内容を置く)か "delete"(追跡ファイルを消す)。
    # src は退避する現在のファイルの実際の名前(無ければ None)、expect はその state 時点の
    # (size, mtime_ns)、staged は txn/new/<n> に展開した内容の (size, mtime_ns)。

    __slots__ = ("expect", "kind", "n", "path", "sha", "src", "staged")

    def __init__(
        self,
        n: int,
        kind: str,
        path: str,
        src: str | None,
        sha: str | None,
        expect: tuple[int, int] | None,
        staged: tuple[int, int] | None = None,
    ):
        self.n = n
        self.kind = kind
        self.path = path
        self.src = src
        self.sha = sha
        self.expect = expect
        self.staged = staged

    def to_json(self) -> dict:
        return {
            "n": self.n,
            "kind": self.kind,
            "path": self.path,
            "src": self.src,
            "sha": self.sha,
            "expect": list(self.expect) if self.expect is not None else None,
            "staged": list(self.staged) if self.staged is not None else None,
        }

    @classmethod
    def from_json(cls, obj: Any, index: int, need_staged: bool) -> _Op:
        # 記録された値を検査する。不正なら ValueError / UnsafePath。
        if not isinstance(obj, dict) or check_id(obj["n"]) != index:
            raise ValueError("ops")
        kind = obj["kind"]
        if kind not in ("write", "delete"):
            raise ValueError("kind")
        path = obj["path"]
        if check_relpath(path) != path:
            raise ValueError("path")
        src = obj["src"]
        if src is not None:
            check_relpath(src)
        sha = check_sha(obj["sha"]) if kind == "write" else None
        if kind == "delete" and src is None:
            raise ValueError("src")

        def pair(v: Any) -> tuple[int, int] | None:
            if v is None:
                return None
            if (
                not isinstance(v, list)
                or len(v) != 2
                or not all(type(x) is int for x in v)
            ):
                raise ValueError("stat")
            return (v[0], v[1])

        expect, staged = pair(obj["expect"]), pair(obj["staged"])
        if (src is None) != (expect is None):
            raise ValueError("expect")
        if kind == "write" and need_staged and staged is None:
            raise ValueError("staged")
        return cls(index, kind, path, src, sha, expect, staged)


def _force_remove(path: Path) -> None:
    # 読み取り専用属性が付いていても消す(P-8)。
    p = os_path(path)
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
    except PermissionError:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        os.remove(p)
