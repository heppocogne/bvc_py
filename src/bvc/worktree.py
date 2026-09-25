# 追跡ファイルの走査・stat キャッシュ・変更検出・復元トランザクション。設計書 1.1節・3.6節。

from __future__ import annotations

import logging
import os
import stat
import time
from pathlib import Path
from typing import Callable, Collection

from .errors import CorruptData, FileBusy, FileChanging, UnsafePath
from .fsutil import (
    atomic_write_json,
    check_relpath,
    check_sha,
    compile_glob,
    is_link_or_reparse,
    load_json,
    new_tmp_path,
    os_path,
    remove_quietly,
)
from .model import Config, PutStats, WorkState
from .store import ObjectStore

logger = logging.getLogger(__name__)

# 書き込み中のファイルの扱い(仕様書 2.5節): 読み取りの前後で stat が変われば、
# RETRY_WAIT 秒おきに読み直し、RETRY_ATTEMPTS 回とも変われば FileChanging。
# テストで待ち時間を短くできるよう、モジュール変数にしている。
RETRY_ATTEMPTS = 3
RETRY_WAIT = 1.0

# stat キャッシュを信用する条件の余裕(FAT の更新日時の粒度。設計書 4.2節、I-14)
FS_TIME_MARGIN_NS = 2_000_000_000


class Worktree:
    # 作業フォルダの追跡ファイルを管理する。bvc_dir は .bvc フォルダ。

    def __init__(self, workdir: Path, bvc_dir: Path, config: Config, store: ObjectStore):
        self.workdir = Path(workdir)
        self._bvc_dir = Path(bvc_dir)
        self._tmp = self._bvc_dir / "tmp"
        self._index_file = self._bvc_dir / "index.json"
        self.config = config
        self.store = store
        self._track = [compile_glob(p) for p in config.track]
        self._ignore = [compile_glob(p) for p in config.ignore]
        self._rules = [
            (compile_glob(r["pattern"]), r.get("chunker", config.chunker), r.get("compression"))
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
                raise FileBusy(f"フォルダを読み取れません: {rel_dir or '.'}({e})", path=rel_dir) from e
            for e in entries:
                if not rel_dir and e.name.casefold() == ".bvc":
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
        except Exception:
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
    ) -> WorkState:
        # 作業フォルダの状態を base_tree と比べる。store_chunks なら変わったファイルを保存する。
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
                raise FileBusy(f"ファイルにアクセスできません: {rel}({e})", path=rel) from e

            ent = index.get(rel)
            if (
                ent is not None
                and rel not in no_cache
                and ent["size"] == st.st_size
                and ent["mtime_ns"] == st.st_mtime_ns
                and ent["mtime_ns"] < index_fs_time - FS_TIME_MARGIN_NS
                and (not store_chunks or self.store.manifest_ok(ent["manifest"], "exists"))
            ):
                tree[rel] = ent["manifest"]
                stats[rel] = (st.st_size, st.st_mtime_ns)
                total_bytes += st.st_size
                continue

            sha, put, st2 = self._hash_file(full, rel, store_chunks)
            tree[rel] = sha
            stats[rel] = (st2.st_size, st2.st_mtime_ns)
            total_bytes += st2.st_size
            new_bytes += put.new_bytes

        modified = sorted(p for p in tree if p in base_tree and base_tree[p] != tree[p])
        added = sorted(p for p in tree if p not in base_tree)
        gone = sorted(p for p in base_tree if p not in tree)

        # 名前変更(内容が完全一致する組だけ。類似度による検知は M4。設計書 4.3節)
        renamed: list[tuple[str, str, float]] = []
        for g in gone:
            n = next((a for a in added if tree[a] == base_tree[g]), None)
            if n is not None:
                renamed.append((g, n, 1.0))
                added.remove(n)
        renamed_from = {r[0] for r in renamed}
        missing = [g for g in gone if g not in renamed_from]

        self._stats = stats
        return WorkState(
            tree=tree,
            modified=modified,
            added=added,
            renamed=renamed,
            missing=missing,
            total_bytes=total_bytes,
            new_bytes=new_bytes,
            fs_time_ns=fs_time_ns,
        )

    def _hash_file(
        self, full: Path, rel: str, store_chunks: bool
    ) -> tuple[str, PutStats, os.stat_result]:
        # stat₁ → 読み込み(分割・ハッシュ・保存)→ stat₂。stat が変わっていれば読み直す。
        chunker, compression = self._rule_for(rel)
        p = os_path(full)
        for attempt in range(RETRY_ATTEMPTS):
            try:
                st1 = os.stat(p)
                f = open(p, "rb")
            except FileNotFoundError as e:
                raise FileChanging(f"読み取り中にファイルが消えました: {rel}", path=rel) from e
            except OSError as e:
                raise FileBusy(
                    f"ファイルを開けません(他のアプリが使用中の可能性があります): {rel}({e})", path=rel
                ) from e
            with f:
                try:
                    if store_chunks:
                        sha, put = self.store.put_file(
                            f, chunker, self.config.verify_chunks, path=rel, compression=compression
                        )
                    else:
                        sha = self.store.hash_file(f, chunker)
                        put = PutStats(size=st1.st_size)
                except OSError as e:
                    raise FileBusy(f"ファイルの読み取り・保存に失敗しました: {rel}({e})", path=rel) from e
            try:
                st2 = os.stat(p)
            except FileNotFoundError as e:
                raise FileChanging(f"読み取り中にファイルが消えました: {rel}", path=rel) from e
            if (st1.st_size, st1.st_mtime_ns) == (st2.st_size, st2.st_mtime_ns) and put.size == st2.st_size:
                return sha, put, st2
            if attempt + 1 < RETRY_ATTEMPTS:
                logger.info("書き込み中のため読み直します: %s", rel)
                time.sleep(RETRY_WAIT)
        raise FileChanging(f"ファイルが書き込み中です(読み取りの前後で変わりました): {rel}", path=rel)

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

    def _load_index(self) -> tuple[dict[str, dict], int]:
        # (entries, fs_time_ns) を返す。無い・壊れていれば空(作り直す)。
        # 知らない format(UnsupportedFormat)と読み込みの OSError はそのまま送出する(D-15)。
        try:
            data = load_json(self._index_file, "index.json")
        except FileNotFoundError:
            return {}, 0
        except CorruptData as e:
            logger.warning("%s(作り直します)", e)
            return {}, 0
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
            logger.warning("index.json: 内容が不正です(作り直します)")
            return {}, 0
        return out, fs_time_ns

    def _mark_fs_time(self) -> int:
        # .bvc/tmp に空の目印ファイルを作り、その更新日時を読んで消す(設計書 2.5節、I-14)。
        mark = new_tmp_path(self._tmp)
        try:
            with open(os_path(mark), "wb"):
                pass
            return os.stat(os_path(mark)).st_mtime_ns
        finally:
            remove_quietly(mark)

    # --- 復元(M3) ---

    def restore(self, target_tree: dict[str, str], on_committed: Callable[[], None]) -> None:
        raise NotImplementedError("復元は M3 で実装する")

    def recover(self) -> None:
        # 中断した復元の後始末(M3 で実装)。M2 では journal が無いので何もしない。
        pass
