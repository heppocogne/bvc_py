# 各操作の手順(トランザクションの組み立て)。設計書 1.1節・3.7節。

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Callable

from .chunkers import make_chunker
from .codecs import choose_codec
from .errors import BvcError, Locked, SafetyAbort, UsageError
from .fsutil import FileLock, atomic_write_json, check_format, load_json, os_path
from .history import History
from .model import Commit, CommitResult, Config, Head, LogEntry, MoveResult, WorkState
from .store import ObjectStore
from .worktree import Worktree


class Repo:
    # リポジトリ(作業フォルダと .bvc)。

    def __init__(
        self,
        workdir: Path,
        repodir: Path,
        config: Config,
        lock: FileLock,
        history: History,
        worktree: Worktree,
        store: ObjectStore,
    ):
        self.workdir = workdir
        self.repodir = repodir
        self.config = config
        self._lock = lock
        self._history = history
        self._worktree = worktree
        self._store = store

    @classmethod
    def find_repodir(cls, start: Path) -> Path | None:
        # カレント(または -C で指定したパス)から上位へ .bvc を探索する。

        current = start
        while True:
            candidate = current / ".bvc"
            if candidate.is_dir():
                return current
            parent = current.parent
            if parent == current:
                return None
            current = parent

    @classmethod
    def init(
        cls,
        workdir: Path,
        track: list[str],
        ignore: list[str] | None = None,
        chunker: dict | None = None,
        compression: str = "auto",
        git: bool = False,
    ) -> Repo:
        # 新しいリポジトリを初期化する(M2-10)。

        workdir = workdir.resolve()
        ignore = ignore or []
        chunker = chunker or {"name": "fixed", "size": 4194304}
        repodir = workdir

        # 既に存在するなら error
        if (repodir / ".bvc").exists():
            raise BvcError("リポジトリは既に存在します")

        # .bvc ディレクトリを作る
        bvc_dir = repodir / ".bvc"
        bvc_dir.mkdir()

        # 管理用フォルダを作る
        (bvc_dir / "commits").mkdir()
        (bvc_dir / "manifests").mkdir()
        (bvc_dir / "chunks").mkdir()
        (bvc_dir / "notes").mkdir()
        (bvc_dir / "quarantine").mkdir()
        (bvc_dir / "txn").mkdir()
        tmp_dir = bvc_dir / "tmp"
        tmp_dir.mkdir()

        # config.json を作る
        config_data = {
            "format": 1,
            "track": track,
            "ignore": ignore,
            "rules": [],
            "chunker": chunker,
            "compression": compression,
            "verify_chunks": "exists",
            "threads": 0,
        }
        config_path = bvc_dir / "config.json"
        atomic_write_json(config_path, config_data, tmp_dir)

        # HEAD.json を作る(版 0)
        head_data = {"format": 1, "at": 0, "branch": 0}
        atomic_write_json(bvc_dir / "HEAD.json", head_data, tmp_dir)

        # counters.json を作る(版 0 から開始)
        counters_data = {"format": 1, "next_commit": 0, "next_branch": 0}
        atomic_write_json(bvc_dir / "counters.json", counters_data, tmp_dir)

        # branches.json を作る
        branches_data = {"format": 1, "names": {}}
        atomic_write_json(bvc_dir / "branches.json", branches_data, tmp_dir)

        # index.json を空で作る
        index_data = {"format": 1, "fs_time_ns": 0, "entries": {}}
        atomic_write_json(bvc_dir / "index.json", index_data, tmp_dir)

        # health.json を空で作る
        health_data = {"format": 1, "bad_chunks": {}, "bad_manifests": {}, "bad_commits": {}}
        atomic_write_json(bvc_dir / "health.json", health_data, tmp_dir)

        # lockstate.json を作る(M6)
        lockstate_data = {"format": 1, "tree_hash": ""}
        atomic_write_json(bvc_dir / "lockstate.json", lockstate_data, tmp_dir)

        # config をパース
        config = cls._load_config(config_data, repodir)

        # ObjectStore を作る
        store = ObjectStore(repodir, config.compression, config.threads)

        # History を作る
        history = History(repodir, store)

        # Worktree を作る
        worktree = Worktree(workdir, repodir, config, store)

        # 版 0(init)を作る
        state = worktree.state(base_tree={}, store_chunks=False)
        init_commit = history.new_commit(
            parent=None,
            tree={},
            kind="init",
            message="",
            renames=(),
            stats={},
        )

        # ロック
        lock = FileLock(bvc_dir / "lock")
        lock.acquire()

        repo = cls(workdir, repodir, config, lock, history, worktree, store)

        # HEAD を版 0 に設定
        history.set_head(Head(at=init_commit.id, branch=0))

        return repo

    @classmethod
    def open(cls, workdir: Path) -> Repo:
        # 既存のリポジトリを開く(M2-10)。

        workdir = workdir.resolve()

        # .bvc を探す
        repodir = cls.find_repodir(workdir)
        if not repodir:
            raise BvcError("リポジトリが見つかりません")

        repodir = repodir.resolve()
        bvc_dir = repodir / ".bvc"

        # ロックを取得
        lock = FileLock(bvc_dir / "lock")
        lock.acquire()

        # config を読む
        config_data = load_json(bvc_dir / "config.json", "config.json")
        config = cls._load_config(config_data, repodir)

        # ObjectStore を作る
        store = ObjectStore(repodir, config.compression, config.threads)

        # History を作る
        history = History(repodir, store)
        history.load()

        # Worktree を作る
        worktree = Worktree(workdir, repodir, config, store)

        # recover を実行(M3 で実装)
        worktree.recover()

        return cls(workdir, repodir, config, lock, history, worktree, store)

    @classmethod
    def _load_config(cls, config_data: dict, repodir: Path) -> Config:
        # config.json をパースして Config インスタンスを作る(M2-2)。

        check_format(config_data, "config.json", known=(1,))

        track = config_data.get("track", [])
        if not isinstance(track, list) or not all(isinstance(p, str) for p in track):
            raise UsageError("track は文字列のリストでなければなりません")

        ignore = config_data.get("ignore", [])
        if not isinstance(ignore, list) or not all(isinstance(p, str) for p in ignore):
            raise UsageError("ignore は文字列のリストでなければなりません")

        rules = config_data.get("rules", [])
        if not isinstance(rules, list):
            raise UsageError("rules はリストでなければなりません")

        chunker = config_data.get("chunker", {"name": "fixed", "size": 4194304})
        if not isinstance(chunker, dict):
            raise UsageError("chunker は辞書でなければなりません")
        # chunker の検査(I-17)
        try:
            make_chunker(chunker)
        except ValueError as e:
            raise UsageError(f"chunker の指定が不正です: {e}")

        compression = config_data.get("compression", "auto")
        if compression not in ("auto", "zlib", "none"):
            raise UsageError(f"compression は 'auto', 'zlib', 'none' のいずれかです: {compression}")
        # compression の検査(I-17)
        try:
            choose_codec(compression, b"")
        except ValueError as e:
            raise UsageError(f"compression の指定が不正です: {e}")

        verify_chunks = config_data.get("verify_chunks", "exists")
        if verify_chunks not in ("exists", "full"):
            raise UsageError(f"verify_chunks は 'exists' か 'full' です: {verify_chunks}")

        threads = config_data.get("threads", 0)
        if not isinstance(threads, int) or threads < 0:
            raise UsageError("threads は非負の整数でなければなりません")

        return Config(
            track=track,
            ignore=ignore,
            rules=rules,
            chunker=chunker,
            compression=compression,
            verify_chunks=verify_chunks,
            threads=threads,
        )

    def close(self) -> None:
        # リポジトリを閉じてロックを解放(M2-10)。

        self._lock.release()

    def __enter__(self) -> Repo:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def work_state(self) -> WorkState:
        # 現在の作業フォルダの状態を取得(M2-10)。

        head = self._history.head()
        current_commit = self._history.get(head.at)
        return self._worktree.state(base_tree=current_commit.tree, store_chunks=False)

    def commit(
        self,
        message: str = "",
        allow_missing: bool = False,
        renames: list[tuple[str, str]] | None = None,
        kind: str = "commit",
        progress: Callable | None = None,
    ) -> CommitResult:
        # コミット(M2-10)。設計書 4.7節の書き込み順で実行する。

        # 現在の HEAD を取得
        head = self._history.head()

        # 作業フォルダの状態を取得
        state = self._worktree.state(base_tree=self._history.get(head.at).tree, store_chunks=True)

        # 欠落の確認
        if state.missing and not allow_missing:
            raise SafetyAbort(f"ファイルが無い: {state.missing}")

        # 変更がなければ changed=False を返す
        if not state.dirty:
            return CommitResult(
                changed=False,
                commit=None,
                state=state,
                new_branch=False,
            )

        # 名前変更の統合(手動指定を含む、M4 で完全実装)
        renames = renames or []
        all_renames = list(renames)

        # 新しい版を作る
        commit = self._history.new_commit(
            parent=head.at,
            tree=state.tree,
            kind=kind,
            message=message,
            renames=tuple(all_renames),
            stats={"new_bytes": 0, "total_bytes": 0},  # 後で充実
        )

        # ブランチを確認して new_branch を設定(設計書 4.5節)
        # 新しく作成された版のブランチが @ のブランチと異なれば新しいブランチ
        new_branch = commit.branch != head.branch

        self._history.set_head(Head(at=commit.id, branch=commit.branch))

        # index を更新(M2-8)
        self._worktree.update_index(state.tree)

        return CommitResult(
            changed=True,
            commit=commit,
            state=state,
            new_branch=new_branch,
        )

    def log(self, include_discarded: bool = False, limit: int | None = None) -> list[LogEntry]:
        # コミット履歴を取得(M2-10)。

        entries = []
        head = self._history.head()

        # 全生きている版を ID の逆順で列挙(新しい順)
        for commit in self._history.living():
            effective_parent = self._history.effective_parent(commit.id)
            branch_label = self._history.branch_name(commit.branch)
            is_tip = self._history.branch_tip(commit.branch) == commit.id
            is_current = commit.id == head.at
            discarded = commit.id in self._history.discarded_ids()
            pinned = commit.id in self._history.pinned_ids()
            notes = self._history.get_notes(commit.id)

            entries.append(
                LogEntry(
                    commit=commit,
                    effective_parent=effective_parent,
                    branch_label=branch_label,
                    is_tip=is_tip,
                    is_current=is_current,
                    discarded=discarded,
                    pinned=pinned,
                    notes=notes,
                )
            )

        if limit:
            entries = entries[:limit]

        return entries
