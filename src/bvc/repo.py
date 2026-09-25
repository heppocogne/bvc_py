# 各操作の手順(トランザクションの組み立て)。設計書 1.1節・3.7節。

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from .chunkers import make_chunker
from .codecs import POLICIES
from .errors import BvcError, MissingFiles, UsageError
from .fsutil import FileLock, atomic_write_json, compile_glob, load_json
from .history import History
from .model import CommitResult, Config, Head, LogEntry, WorkState
from .store import ObjectStore
from .worktree import Worktree

logger = logging.getLogger(__name__)

BVC_DIR = ".bvc"
DEFAULT_CHUNKER: dict = {"name": "fixed", "size": 4194304}
_SUBDIRS = ("commits", "manifests", "chunks", "notes", "quarantine", "txn", "tmp")


def _config_error(msg: str) -> UsageError:
    return UsageError(f"config.json: {msg}")


def parse_config(data: dict) -> Config:
    # config.json の内容を検査して Config にする(M2-2)。不正な指定は設定エラー(I-17)。
    track = data.get("track")
    if not isinstance(track, list) or not track or not all(type(p) is str and p for p in track):
        raise _config_error("track は空でない文字列のリストにしてください")
    ignore = data.get("ignore", [])
    if not isinstance(ignore, list) or not all(type(p) is str and p for p in ignore):
        raise _config_error("ignore は文字列のリストにしてください")

    def check_chunker(ck: Any, where: str) -> dict:
        if not isinstance(ck, dict):
            raise _config_error(f"{where} は辞書にしてください")
        try:
            make_chunker(ck)
        except (ValueError, TypeError) as e:
            raise _config_error(f"{where} の指定が不正です: {e}") from None
        return ck

    def check_compression(c: Any, where: str) -> str:
        if c not in POLICIES:
            raise _config_error(f"{where} は {', '.join(POLICIES)} のいずれかにしてください: {c!r}")
        return c

    chunker = check_chunker(data.get("chunker", DEFAULT_CHUNKER), "chunker")
    compression = check_compression(data.get("compression", "auto"), "compression")

    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise _config_error("rules はリストにしてください")
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict) or type(r.get("pattern")) is not str or not r["pattern"]:
            raise _config_error(f"{where} には pattern(文字列)が必要です")
        if "chunker" in r:
            check_chunker(r["chunker"], f"{where}.chunker")
        if "compression" in r:
            check_compression(r["compression"], f"{where}.compression")

    verify_chunks = data.get("verify_chunks", "exists")
    if verify_chunks not in ("exists", "full"):
        raise _config_error(f"verify_chunks は exists か full にしてください: {verify_chunks!r}")
    threads = data.get("threads", 0)
    if type(threads) is not int or threads < 0:
        raise _config_error("threads は 0 以上の整数にしてください")

    for p in track + ignore + [r["pattern"] for r in rules]:
        compile_glob(p)
    return Config(
        track=list(track),
        ignore=list(ignore),
        rules=list(rules),
        chunker=chunker,
        compression=compression,
        verify_chunks=verify_chunks,
        threads=threads,
    )


class Repo:
    # リポジトリ。workdir は作業フォルダ(.bvc のあるフォルダ)、bvc_dir はその中の .bvc。

    def __init__(self, workdir: Path, config: Config, lock: FileLock):
        self.workdir = workdir
        self.bvc_dir = workdir / BVC_DIR
        self.config = config
        self._lock = lock
        self._store = ObjectStore(self.bvc_dir, config.compression, config.threads)
        self._history = History(self.bvc_dir)
        self._worktree = Worktree(workdir, self.bvc_dir, config, self._store)

    # --- 開く・閉じる ---

    @staticmethod
    def find_workdir(start: Path) -> Path | None:
        # start から上位へ .bvc を探し、それを含むフォルダを返す(M2-2)。
        current = Path(start).resolve()
        while True:
            if (current / BVC_DIR).is_dir():
                return current
            if current.parent == current:
                return None
            current = current.parent

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
        # 新しいリポジトリを作り、その時点の追跡ファイルを版 0(kind=init)として記録する(M2-10)。
        if git:
            raise UsageError("git 連携は M6 で実装します")
        workdir = Path(workdir).resolve()
        if not workdir.is_dir():
            raise BvcError(f"作業フォルダがありません: {workdir}")
        config_data = {
            "format": 1,
            "track": list(track),
            "ignore": list(ignore or []),
            "rules": [],
            "chunker": chunker or dict(DEFAULT_CHUNKER),
            "compression": compression,
            "verify_chunks": "exists",
            "threads": 0,
        }
        config = parse_config(config_data)
        found = cls.find_workdir(workdir)
        if found is not None:
            raise BvcError(f"すでにリポジトリがあります: {found / BVC_DIR}")

        bvc_dir = workdir / BVC_DIR
        bvc_dir.mkdir()
        # 作ったばかりの .bvc だけを片付けの対象にする(作業ファイルには触れない)
        lock = FileLock(bvc_dir / "lock")
        repo: Repo | None = None
        try:
            lock.acquire()
            for d in _SUBDIRS:
                (bvc_dir / d).mkdir()
            tmp = bvc_dir / "tmp"
            atomic_write_json(bvc_dir / "config.json", config_data, tmp)
            atomic_write_json(bvc_dir / "counters.json", {"format": 1, "next_commit": 0, "next_branch": 0}, tmp)
            atomic_write_json(bvc_dir / "branches.json", {"format": 1, "names": {}}, tmp)
            atomic_write_json(
                bvc_dir / "health.json",
                {"format": 1, "bad_chunks": {}, "bad_manifests": {}, "bad_commits": {}},
                tmp,
            )
            repo = cls(workdir, config, lock)
            repo._history.load()
            state = repo._worktree.state(base_tree={}, store_chunks=True)
            if not state.tree:
                logger.warning("追跡対象のファイルがありません(パターン: %s)", ", ".join(config.track))
            commit = repo._history.new_commit(
                parent=None,
                tree=state.tree,
                kind="init",
                message="",
                stats={"new_bytes": state.new_bytes, "total_bytes": state.total_bytes},
            )
            repo._history.set_head(Head(at=commit.id, branch=commit.branch))
            repo._worktree.update_index(state.tree, state.fs_time_ns)
            repo._history.log_op(
                {"op": "init", "args": {}, "before": None, "after": {"at": commit.id, "branch": commit.branch},
                 "created": [commit.id], "result": "ok"}
            )
        except BaseException:
            if repo is not None:
                repo._store.close()
            lock.release()
            shutil.rmtree(bvc_dir, ignore_errors=True)
            raise
        return repo

    @classmethod
    def open(cls, start: Path) -> Repo:
        # start から上位へ .bvc を探して開く(M2-10)。ロック取得 → recover → History.load。
        workdir = cls.find_workdir(start)
        if workdir is None:
            raise BvcError(f"リポジトリが見つかりません({Path(start).resolve()} とその上位に .bvc がありません)")
        bvc_dir = workdir / BVC_DIR
        lock = FileLock(bvc_dir / "lock")
        lock.acquire()
        repo: Repo | None = None
        try:
            config = parse_config(load_json(bvc_dir / "config.json", "config.json"))
            repo = cls(workdir, config, lock)
            repo._worktree.recover()
            repo._history.load()
        except BaseException:
            if repo is not None:
                repo._store.close()
            lock.release()
            raise
        return repo

    def close(self) -> None:
        # ロックを解放する(M2-10)。
        try:
            self._store.close()
        finally:
            self._lock.release()

    def __enter__(self) -> Repo:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 操作 ---

    def work_state(self) -> WorkState:
        # 未コミットの変更を調べる(保存はしない)。
        head = self._history.head()
        return self._worktree.state(base_tree=self._history.get(head.at).tree, store_chunks=False)

    def commit(
        self,
        message: str = "",
        allow_missing: bool = False,
        renames: list[tuple[str, str]] | None = None,
        kind: str = "commit",
        progress: Callable | None = None,
    ) -> CommitResult:
        # 追跡ファイルの現状を新しい版として記録する(M2-10)。
        # 書き込み順: チャンク → マニフェスト → counters → 版 → HEAD → (bvc.lock: M6) → index → oplog(設計書 4.7節)。
        if renames:
            raise UsageError("名前変更の手動指定(--rename)は M4 で実装します")
        head = self._history.head()
        base = self._history.get(head.at)
        state = self._worktree.state(base_tree=base.tree, store_chunks=True)

        if state.missing and not allow_missing:
            raise MissingFiles(
                "追跡ファイルが見つかりません\n"
                + "".join(f"  missing: {p}\n" for p in state.missing)
                + "  削除として記録するには --allow-missing を指定してください",
                missing=list(state.missing),
            )

        if not state.dirty:
            # 内容が同じなら記録(stat キャッシュ)だけ更新する(仕様書 2.5節)
            self._worktree.update_index(state.tree, state.fs_time_ns)
            return CommitResult(changed=False, commit=None, state=state)

        commit = self._history.new_commit(
            parent=head.at,
            tree=state.tree,
            kind=kind,
            message=message,
            renames=tuple(state.renamed),
            stats={"new_bytes": state.new_bytes, "total_bytes": state.total_bytes},
        )
        new_branch = commit.branch != head.branch
        after = Head(at=commit.id, branch=commit.branch)
        self._history.set_head(after)
        self._worktree.update_index(state.tree, state.fs_time_ns)
        self._history.log_op(
            {"op": kind, "args": {"message": message, "allow_missing": allow_missing},
             "before": {"at": head.at, "branch": head.branch},
             "after": {"at": after.at, "branch": after.branch},
             "created": [commit.id], "result": "ok"}
        )
        return CommitResult(changed=True, commit=commit, state=state, new_branch=new_branch)

    def resolve(self, rev: str) -> int:
        # リビジョン式を版番号にする。
        return self._history.resolve(rev, self._history.head())

    def log(self, include_discarded: bool = False, limit: int | None = None) -> list[LogEntry]:
        # 版を新しい順に返す(M2-10)。読めない版も含める(commit=None)。
        h = self._history
        head = h.head()
        pinned = h.pinned_ids()
        entries = []
        for cid in h.ids(include_discarded):
            c = h.get(cid) if h.is_readable(cid) else None
            is_tip = c is not None and h.branch_tip(c.branch) == cid
            entries.append(
                LogEntry(
                    id=cid,
                    commit=c,
                    effective_parent=h.effective_parent(cid),
                    branch_label=h.branch_name(c.branch) if is_tip else None,
                    is_tip=is_tip,
                    is_current=cid == head.at,
                    broken=h.is_broken(cid),
                    discarded=h.is_discarded(cid),
                    pinned=cid in pinned,
                    notes=h.get_notes(cid),
                )
            )
        if limit is not None:
            entries = entries[:limit]
        return entries
