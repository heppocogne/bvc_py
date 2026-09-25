# 追跡ファイルの走査・stat キャッシュ・変更検出・復元トランザクション。設計書 1.1節・3.6節。

from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path
from typing import Callable, Collection

from .errors import FileBusy, FileChanging, MissingFiles, UnsafePath
from .fsutil import (
    atomic_write_json,
    check_relpath,
    compile_glob,
    load_json,
    os_path,
    resolve_in_workdir,
)
from .model import Config, WorkState
from .store import ObjectStore


class Worktree:
    # 作業フォルダの追跡ファイルを管理する。

    def __init__(self, workdir: Path, repodir: Path, config: Config, store: ObjectStore):
        self.workdir = workdir.resolve()
        self.repodir = repodir.resolve()
        self.config = config
        self.store = store

        # glob パターンをコンパイル
        self._track_patterns = [compile_glob(p) for p in config.track]
        self._ignore_patterns = [compile_glob(p) for p in config.ignore]

    def scan(self) -> list[str]:
        # 追跡対象のパスを列挙(M2-7)。設計書 4.1節。

        matched = []
        seen_lower = {}  # 大文字小文字を無視したパスの記録(衝突検出用)

        for root, dirs, files in os.walk(os_path(self.workdir)):
            root_path = Path(root)

            # サブフォルダをフィルタ
            dirs_to_keep = []
            for d in dirs:
                full_path = root_path / d
                # リンク・リパースポイントをスキップ
                try:
                    stat_info = os.lstat(full_path)
                    if os.path.islink(full_path):
                        continue
                    # Windows の reparse point(ジャンクション等)をスキップ
                    if hasattr(stat_info, "st_file_attributes") and stat_info.st_file_attributes & 0x400:
                        continue
                except OSError:
                    continue

                dirs_to_keep.append(d)

            dirs[:] = dirs_to_keep

            # ファイルをチェック
            for f in files:
                full_path = root_path / f

                # リンク・リパースポイントをスキップ
                try:
                    if os.path.islink(full_path):
                        continue
                except OSError:
                    continue

                # 相対パス(NFC 正規化、/ 区切り)に変換
                try:
                    rel_path = full_path.relative_to(self.workdir)
                    rel_str = str(rel_path).replace("\\", "/")
                    rel_str = check_relpath(rel_str)  # 検査と正規化
                except (ValueError, UnsafePath):
                    continue

                # 追跡対象か確認
                matched_track = any(p.fullmatch(rel_str) for p in self._track_patterns)
                matched_ignore = any(p.fullmatch(rel_str) for p in self._ignore_patterns)

                if not matched_track or matched_ignore:
                    continue

                # 大文字小文字の衝突をチェック
                rel_lower = rel_str.lower()
                if rel_lower in seen_lower:
                    if seen_lower[rel_lower] != rel_str:
                        raise UnsafePath(f"大文字小文字だけ異なるパスの衝突: {seen_lower[rel_lower]} と {rel_str}")
                else:
                    seen_lower[rel_lower] = rel_str

                matched.append(rel_str)

        return sorted(matched)

    def state(
        self,
        base_tree: dict[str, str] | int,
        store_chunks: bool,
        no_cache: Collection[str] = (),
    ) -> WorkState:
        # 作業フォルダの状態を取得(M2-9)。設計書 4.2節。

        if isinstance(base_tree, int):
            # base_tree が版 ID の場合(将来用)
            base_tree = {}

        # index を読む
        index = self._load_index()

        # FS 時刻の目印ファイルを作る(I-14)
        fs_time_ns = self._mark_fs_time()

        # 追跡ファイルを走査
        scan_result = self.scan()

        # 現状のツリーを作る
        current_tree = {}
        modified = []
        added = []
        missing = []

        for rel_path in scan_result:
            full_path = resolve_in_workdir(self.workdir, rel_path)

            # ファイルが存在するか確認
            try:
                stat1 = os.stat(os_path(full_path))
            except FileNotFoundError:
                missing.append(rel_path)
                continue
            except OSError:
                raise FileBusy(f"ファイルにアクセスできません: {rel_path}")

            # index からマニフェスト sha を取得(M2-8)
            if rel_path in index and rel_path not in no_cache:
                entry = index[rel_path]
                # stat が変わっていないか確認(I-14)
                if (
                    entry["size"] == stat1.st_size
                    and entry["mtime_ns"] == stat1.st_mtime_ns
                    and entry["mtime_ns"] < fs_time_ns - 2_000_000_000  # 2秒
                ):
                    # マニフェストを再利用
                    current_tree[rel_path] = entry["manifest"]
                    continue

            # ハッシュして保存(M2-8)
            try:
                manifest_sha = self._hash_and_save_file(full_path, rel_path, store_chunks, fs_time_ns)
                current_tree[rel_path] = manifest_sha

                if rel_path in base_tree:
                    if base_tree[rel_path] != manifest_sha:
                        modified.append(rel_path)
                else:
                    added.append(rel_path)

            except FileChanging:
                raise FileChanging(f"ファイルが頻繁に書き換わります: {rel_path}")

        # 消えたファイル
        for path in base_tree:
            if path not in current_tree:
                missing.append(path)

        # 名前変更の検知は M4 で実装

        result = WorkState(
            tree=current_tree,
            modified=modified,
            added=added,
            renamed=[],
            missing=missing,
            hints={},
        )

        return result

    def restore(
        self,
        target_tree: dict[str, str],
        on_committed: Callable[[], None],
    ) -> None:
        # 復元トランザクション(M3 で実装)。

        pass

    def recover(self) -> None:
        # 中断した復元の後始末(M3 で実装)。

        pass

    def update_index(self, tree: dict[str, str]) -> None:
        # index を更新(M2-8)。

        index_file = self.repodir / ".bvc" / "index.json"
        tmp_dir = self.repodir / ".bvc" / "tmp"

        # FS 時刻を記録
        fs_time_ns = self._mark_fs_time()

        # entries を作る
        entries = {}
        for rel_path, manifest_sha in tree.items():
            full_path = resolve_in_workdir(self.workdir, rel_path)

            try:
                stat_info = os.stat(os_path(full_path))
                entries[rel_path] = {
                    "size": stat_info.st_size,
                    "mtime_ns": stat_info.st_mtime_ns,
                    "manifest": manifest_sha,
                }
            except (FileNotFoundError, OSError):
                # ファイルが無ければ index に入れない
                pass

        index_data = {
            "format": 1,
            "fs_time_ns": fs_time_ns,
            "entries": entries,
        }

        atomic_write_json(index_file, index_data, tmp_dir)

    # ヘルパー関数

    def _load_index(self) -> dict[str, dict]:
        # index.json を読む(M2-8)。壊れていたら空扱い。

        index_file = self.repodir / ".bvc" / "index.json"

        try:
            data = load_json(index_file, "index.json")
            entries = data.get("entries", {})
            return entries
        except FileNotFoundError:
            return {}
        except Exception:
            # 壊れていたら空
            return {}

    def _mark_fs_time(self) -> int:
        # FS 時刻の目印ファイルを作り、mtime_ns を返す(I-14)。

        tmp_dir = self.repodir / ".bvc" / "tmp"
        tmp_dir.mkdir(exist_ok=True)

        # 空ファイルを作る
        mark_file = tmp_dir / ".fs_time"
        try:
            mark_file.touch()
            stat_info = os.stat(os_path(mark_file))
            fs_time_ns = stat_info.st_mtime_ns
            mark_file.unlink()
            return fs_time_ns
        except OSError:
            # 作成に失敗したら現在時刻を返す(フォールバック)
            return int(time.time() * 1_000_000_000)

    def _hash_and_save_file(
        self,
        full_path: Path,
        rel_path: str,
        store_chunks: bool,
        fs_time_ns: int,
    ) -> str:
        # ファイルをハッシュして保存(M2-9)。設計書 4.2節の stat₁ → 読み込み → stat₂。

        for attempt in range(3):
            stat1 = os.stat(os_path(full_path))

            try:
                with open(os_path(full_path), "rb") as f:
                    if store_chunks:
                        # マニフェストを作って保存
                        manifest_sha, _ = self.store.put_file(f, self.config.chunker, "exists")
                    else:
                        # マニフェストだけを計算
                        manifest_sha = self.store.hash_file(f, self.config.chunker)

            except OSError as e:
                if attempt < 2:
                    time.sleep(1)
                    continue
                else:
                    raise FileBusy(f"ファイルにアクセスできません: {rel_path}")

            # stat₂ を確認
            try:
                stat2 = os.stat(os_path(full_path))
            except FileNotFoundError:
                raise FileBusy(f"ファイルが削除されました: {rel_path}")

            if stat1.st_size == stat2.st_size and stat1.st_mtime_ns == stat2.st_mtime_ns:
                return manifest_sha

            # 変わっていれば再試行
            if attempt < 2:
                time.sleep(1)
                continue
            else:
                raise FileChanging(f"ファイルが頻繁に書き換わります: {rel_path}")

        raise FileChanging(f"ファイルが頻繁に書き換わります: {rel_path}")
