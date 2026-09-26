# 各操作の手順(トランザクションの組み立て)。設計書 1.1節・3.7節。

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from . import fsutil
from .chunkers import make_chunker
from .codecs import POLICIES
from .errors import (
    BrokenVersion,
    BvcError,
    CannotMove,
    CorruptData,
    FileBusy,
    MissingFiles,
    PinnedCommit,
    RevisionError,
    UsageError,
)
from .fsutil import FileLock, atomic_write_json, compile_glob, load_json
from .history import History, check_branch_name
from .model import (
    BranchInfo,
    Commit,
    CommitResult,
    Config,
    DiscardResult,
    GcReport,
    Head,
    LogEntry,
    MoveResult,
    Note,
    ProgressEvent,
    WorkState,
)
from .store import ObjectStore, ProgressFn
from .worktree import Worktree

logger = logging.getLogger(__name__)

BVC_DIR = ".bvc"
DEFAULT_CHUNKER: dict = {"name": "fixed", "size": 4194304}
_SUBDIRS = ("commits", "manifests", "chunks", "notes", "quarantine", "txn", "tmp")


def _config_error(msg: str) -> UsageError:
    return UsageError(f"config.json: {msg}")


def _missing_error(state: WorkState) -> MissingFiles:
    return MissingFiles(
        "追跡ファイルが見つかりません\n"
        + "".join(f"  missing: {p}\n" for p in state.missing)
        + "  削除として記録するには --allow-missing を指定してください",
        missing=list(state.missing),
    )


def _no_skip_broken(skip_broken: bool) -> None:
    if skip_broken:
        raise UsageError("壊れた版を飛ばす移動(--skip-broken)は M4 で実装します")


def _head_json(head: Head) -> dict:
    return {"at": head.at, "branch": head.branch}


def _op_entry(op: str, args: dict, head: Head) -> dict:
    # 現在位置を変えない操作の oplog の記録。
    return {"op": op, "args": args, "reason": "", "before": _head_json(head),
            "after": _head_json(head), "created": [], "result": "ok"}


def _check_name(name: str) -> str:
    # ブランチ名の誤りは引数の誤り(終了コード 2)にする。
    try:
        return check_branch_name(name)
    except RevisionError as e:
        raise UsageError(str(e)) from None


def _file_size(path: Path) -> int:
    try:
        return os.lstat(fsutil.os_path(path)).st_size
    except FileNotFoundError:
        return 0


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
            repo._worktree.recover(repo._history.set_head)
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
            raise _missing_error(state)

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
             "before": _head_json(head), "after": _head_json(after),
             "created": [commit.id], "result": "ok"}
        )
        return CommitResult(changed=True, commit=commit, state=state, new_branch=new_branch)

    # --- 移動系(M3-6, M3-7、設計書 4.1節・4.5節) ---

    def undo(
        self,
        reason: str = "",
        allow_missing: bool = False,
        skip_broken: bool = False,
        progress: ProgressFn | None = None,
    ) -> MoveResult:
        # 親の版(effective_parent)へ移動する。現在のブランチ(redo の方向)は変えない。
        _no_skip_broken(skip_broken)
        head = self._history.head()
        target = self._history.effective_parent(head.at)
        if target is None:
            raise CannotMove(f"版 {head.at} は根(親の無い版)なので、これ以上戻れません", at=head.at)
        return self._move("undo", "", reason, target, None, allow_missing, progress)

    def redo(
        self,
        reason: str = "",
        allow_missing: bool = False,
        skip_broken: bool = False,
        progress: ProgressFn | None = None,
    ) -> MoveResult:
        # 現在のブランチの先端へ向かって1つ進む。経路上に無ければ、子が1つのときだけ進む。
        _no_skip_broken(skip_broken)
        h = self._history
        head = h.head()
        path = h.path_to_tip(head.branch)
        if head.at in path:
            idx = path.index(head.at)
            if idx == 0:
                raise CannotMove(f"版 {head.at} はブランチの先端なので、これ以上進めません", at=head.at)
            return self._move("redo", "", reason, path[idx - 1], head.branch, allow_missing, progress)
        kids = h.children(head.at)
        if not kids:
            raise CannotMove(f"版 {head.at} は先端(子の無い版)なので、これ以上進めません", at=head.at)
        if len(kids) > 1:
            raise CannotMove(
                f"版 {head.at} には子が複数あるため、進む先を決められません。"
                f"goto で版を指定してください(候補: {', '.join(map(str, kids))})",
                at=head.at,
                candidates=kids,
            )
        self._check_movable(kids[0])
        return self._move("redo", "", reason, kids[0], h.get(kids[0]).branch, allow_missing, progress)

    def goto(
        self, rev: str, allow_missing: bool = False, progress: ProgressFn | None = None
    ) -> MoveResult:
        # 指定の版へ移動する。ブランチ名ならそのブランチを、版番号ならその版のブランチを現在のブランチにする。
        # 現在位置への goto は何もしない(changed=False)。
        h = self._history
        head = h.head()
        target = h.resolve(rev, head)
        named = h.branch_by_name(rev)
        if named is not None:
            branch = named
        elif target == head.at:
            branch = head.branch
        else:
            self._check_movable(target)
            branch = h.get(target).branch
        if target == head.at:
            if branch == head.branch:
                return MoveResult(changed=False, before=head, after=head)
            # 位置は同じで、ブランチ(redo の方向)だけを切り替える
            after = Head(head.at, branch)
            h.set_head(after)
            h.log_op(
                {"op": "goto", "args": {"rev": rev}, "reason": "", "before": _head_json(head),
                 "after": _head_json(after), "created": [], "result": "ok"}
            )
            return MoveResult(changed=True, before=head, after=after)
        return self._move("goto", rev, "", target, branch, allow_missing, progress, {"rev": rev})

    def _check_movable(self, target: int) -> None:
        # 移動先の版が読めて、tree に不正な値が無いか(マニフェスト・チャンクは restore の事前検査で確かめる)。
        if not self._history.exists(target):
            raise RevisionError(f"版 {target} は存在しません")
        if self._history.is_broken(target):
            raise BrokenVersion(f"版 {target} は壊れているため移動できません", commit=target)

    def _move(
        self,
        op: str,
        arg: str,
        reason: str,
        target_id: int,
        branch: int | None,
        allow_missing: bool,
        progress: ProgressFn | None,
        args: dict | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> MoveResult:
        # 移動系の共通手順(設計書 4.1節)。移動先は呼び出し側が自動コミットの前に決めておく。
        # branch が None なら、移動後もそのときの HEAD.branch(自動コミットがあればそのブランチ)を保つ。
        # on_done は復元が成功した後、oplog の前に呼び出す(discard の削除印など)。
        h, wt = self._history, self._worktree
        head = h.head()
        self._check_movable(target_id)
        target = h.get(target_id)
        base = h.get(head.at)

        # 事前検査(衝突・パス・保存データ)。ここまでは何も変えない
        files = wt.check_target(target.tree)
        paths = set(base.tree) | set(target.tree) | set(files)
        # 上書き・削除し得るパスは、stat キャッシュを使わずにハッシュする(仕様書 2.8節)
        touched = {p for p in paths if base.tree.get(p) != target.tree.get(p) or p not in target.tree}
        state = wt.state(base_tree=base.tree, store_chunks=True, no_cache=touched)
        if state.missing and not allow_missing:
            raise _missing_error(state)

        # 自動コミット。HEAD も auto に進めてから復元する(I-18)
        auto = None
        current = head
        if state.dirty:
            message = f"auto: before {op}" + (f" {arg}" if arg else "")
            auto = h.new_commit(
                parent=head.at,
                tree=state.tree,
                kind="auto",
                message=message,
                renames=tuple(state.renamed),
                stats={"new_bytes": state.new_bytes, "total_bytes": state.total_bytes},
            )
            current = Head(auto.id, auto.branch)
            h.set_head(current)
            wt.update_index(state.tree, state.fs_time_ns)

        after = Head(target_id, current.branch if branch is None else branch)
        entry = {
            "op": op,
            "args": {**(args or {}), "allow_missing": allow_missing},
            "reason": reason,
            "before": _head_json(head),
            "created": [auto.id] if auto is not None else [],
        }
        try:
            res = wt.restore(target.tree, state, after, h.set_head, progress)
        except BaseException as e:
            if auto is not None:
                # 自動コミットで履歴は変わったので、中止したことも記録する
                try:
                    h.log_op({**entry, "after": _head_json(current), "result": "error", "error": str(e)})
                except Exception:
                    logger.warning("操作ログに記録できませんでした", exc_info=True)
            raise
        if on_done is not None:
            on_done()
        h.log_op({**entry, "after": _head_json(after), "result": "ok"})
        return MoveResult(
            changed=True,
            before=head,
            after=after,
            auto_commit=auto,
            restored=res.written,
            deleted=res.deleted,
        )

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

    # --- 履歴操作(M4-1〜M4-4) ---

    def get_commit(self, commit_id: int) -> Commit | None:
        # 版の内容(表示用)。読み込み不可の版は None。
        h = self._history
        if not h.exists(commit_id):
            raise RevisionError(f"版 {commit_id} は存在しません")
        return h.get(commit_id) if h.is_readable(commit_id) else None

    def note(self, text: str, rev: str = "@") -> Note:
        # 版にコメントを追記する(M4-1、仕様書 3.6節)。読み込み不可・壊れた版にも付けられる。
        if type(text) is not str or not text:
            raise UsageError("コメントの本文が空です")
        h = self._history
        head = h.head()
        commit_id = h.resolve(rev, head)
        note = h.add_note(commit_id, text)
        h.log_op(_op_entry("note", {"rev": rev, "id": commit_id}, head))
        return note

    def branches(self) -> list[BranchInfo]:
        # ブランチの一覧(M4-2、仕様書 3.7節)。生きている版のあるブランチ、名前の付いたブランチ、現在のブランチ。
        h = self._history
        head = h.head()
        numbers = {c.branch for c in h.living()} | set(h.branch_names()) | {head.branch}
        return [self._branch_info(b, head) for b in sorted(numbers)]

    def _branch_info(self, branch: int, head: Head) -> BranchInfo:
        # 分岐元: 先端からつなぎ直し後の親をたどり、別のブランチ(または読み込み不可の版)に出た所。
        h = self._history
        tip = h.branch_tip(branch)
        fork = None
        cur = tip
        while cur is not None:
            p = h.effective_parent(cur)
            if p is None or not h.is_readable(p) or h.get(p).branch != branch:
                fork = p
                break
            cur = p
        return BranchInfo(
            number=branch, name=h.branch_name(branch), tip=tip, fork=fork, is_current=branch == head.branch
        )

    def name_branch(self, name: str, rev: str = "@") -> BranchInfo:
        # rev が属するブランチに名前を付ける(M4-2)。同じ名前が別のブランチにあれば付け替える。
        _check_name(name)
        h = self._history
        head = h.head()
        commit_id = h.resolve(rev, head)
        if not h.is_readable(commit_id):
            raise BvcError(f"版 {commit_id} は読み込めないため、属するブランチが分かりません")
        branch = h.get(commit_id).branch
        previous = h.name_branch(branch, name)
        if previous is not None:
            tip = h.branch_tip(previous)
            logger.info("名前 '%s' を別のブランチ(先端 %s)から付け替えました", name, "なし" if tip is None else tip)
        h.log_op(_op_entry("branch_name", {"name": name, "rev": rev, "branch": branch, "previous": previous}, head))
        return self._branch_info(branch, head)

    def unname_branch(self, name: str) -> BranchInfo:
        # 名前を外す(M4-2)。名前を外したブランチの情報を返す。
        _check_name(name)
        h = self._history
        head = h.head()
        branch = h.unname_branch(name)
        h.log_op(_op_entry("branch_unname", {"name": name, "branch": branch}, head))
        return self._branch_info(branch, head)

    def discard(
        self,
        rev: str = "@",
        force: bool = False,
        allow_missing: bool = False,
        progress: ProgressFn | None = None,
    ) -> DiscardResult:
        # 版に削除印を付ける(M4-3、仕様書 3.8節)。データは gc まで残る。子は親につなぎ直される。
        # 現在位置なら、親へ移動してから削除印を付ける。未コミットの変更は自動コミット(消す版の子)に残る。
        # 削除印は復元が成功した後に書く。途中で失敗・中断しても「移動も削除もしていない」か
        # 「移動だけした」状態で終わり、HEAD が削除済みの版を指すことはない。
        h = self._history
        head = h.head()
        commit_id = h.resolve(rev, head)
        if h.is_discarded(commit_id):
            raise RevisionError(f"版 {commit_id} は削除済みです")
        if commit_id in h.pinned_ids() and not force:
            raise PinnedCommit(
                f"版 {commit_id} は git のコミットから参照されています。削除するには --force を指定してください",
                commit=commit_id,
            )
        args = {"rev": rev, "id": commit_id, "force": force}
        if commit_id != head.at:
            h.discard(commit_id)
            h.log_op(_op_entry("discard", {**args, "allow_missing": allow_missing}, head))
            return DiscardResult(changed=True, discarded=commit_id, before=head, after=head)

        parent = h.effective_parent(commit_id)
        if parent is None:
            raise CannotMove(
                f"版 {commit_id} は根(親の無い版)なので、現在位置のまま削除できません。別の版へ移動してから削除してください",
                at=commit_id,
            )
        res = self._move(
            "discard", "" if rev == "@" else rev, "", parent, None, allow_missing, progress, args,
            on_done=lambda: h.discard(commit_id),
        )
        return DiscardResult(
            changed=True,
            discarded=commit_id,
            before=res.before,
            after=res.after,
            auto_commit=res.auto_commit,
            restored=res.restored,
            deleted=res.deleted,
        )

    def gc(self, dry_run: bool = False, no_git: bool = False, progress: ProgressFn | None = None) -> GcReport:
        # 削除済みの版と、参照されないマニフェスト・チャンク・一時ファイルを消す(M4-4、設計書 4.8節)。
        # git 連携(M6)が無い間は、no_git に関係なく git の履歴は調べない。
        # 削除順は 版 → マニフェスト → チャンク → 一時ファイル。途中で止まっても、残るのは参照されないものだけ(R-8)。
        # 参照先を把握できない版・マニフェストがあれば、その先の削除は見送る(D-15)。
        h, s = self._history, self._store
        head = h.head()
        pinned = h.pinned_ids()
        ids = h.ids(include_discarded=True)
        garbage = [c for c in ids if h.is_discarded(c) and c not in pinned and c != head.at]
        kept = [c for c in ids if c not in set(garbage)]

        # mark
        unknown = [c for c in kept if not h.tree_known(c)]
        manifests: set[str] = set()
        for c in kept:
            if h.tree_known(c):
                manifests.update(h.get(c).tree.values())
        chunks: set[str] = set()
        unreadable = []
        for sha in sorted(manifests):
            try:
                m = s.get_manifest(sha)
            except CorruptData:
                unreadable.append(sha)
                continue
            except OSError as e:
                raise FileBusy(f"マニフェストを読めません({e}): {sha}", sha=sha) from e
            chunks.update(ref.sha for ref in m.chunks)
        skipped = []
        if unknown:
            skipped = ["manifests", "chunks"]
            logger.warning(
                "版 %s は読み込めない(または不正な記録を含む)ため、参照するデータが分かりません。"
                "安全のため、マニフェストとチャンクは削除しません(その版を discard すると削除できます)",
                ", ".join(map(str, sorted(unknown))),
            )
        elif unreadable:
            skipped = ["chunks"]
            logger.warning(
                "生きている版が参照するマニフェスト %d 件を読めないため、参照するチャンクが分かりません。"
                "安全のため、チャンクは削除しません",
                len(unreadable),
            )

        # 削除の対象
        known = set(ids)
        orphan_notes = [h.commit_paths(c)[1] for c in h.note_ids() if c not in known]
        del_manifests = [] if "manifests" in skipped else [m for m in s.iter_manifests() if m not in manifests]
        del_chunks = [] if "chunks" in skipped else [c for c in s.iter_chunks() if c not in chunks]
        tmp_files = self._tmp_files()
        freed = (
            sum(_file_size(p) for c in garbage for p in h.commit_paths(c))
            + sum(_file_size(p) for p in orphan_notes)
            + sum(_file_size(s.manifest_path(m)) for m in del_manifests)
            + sum(_file_size(s.chunk_path(c)) for c in del_chunks)
            + sum(_file_size(p) for p in tmp_files)
        )
        report = GcReport(
            changed=False,
            dry_run=dry_run,
            deleted_commits=sorted(garbage),
            deleted_manifests=len(del_manifests),
            deleted_chunks=len(del_chunks),
            deleted_tmp=len(tmp_files),
            freed_bytes=freed,
            skipped=skipped,
        )
        total = len(garbage) + len(orphan_notes) + len(del_manifests) + len(del_chunks) + len(tmp_files)
        if dry_run or total == 0:
            return report

        # sweep
        done = 0

        def step() -> None:
            nonlocal done
            done += 1
            if progress is not None:
                progress(ProgressEvent("gc", done, total))

        try:
            for c in sorted(garbage):
                fsutil.fault("gc:commit")
                for p in h.commit_paths(c):
                    fsutil.remove_quietly(p)
                s.health.clear("bad_commits", str(c))
                step()
            for p in orphan_notes:
                fsutil.fault("gc:note")
                fsutil.remove_quietly(p)
                step()
            for m in del_manifests:
                fsutil.fault("gc:manifest")
                s.delete_manifest(m)
                step()
            for c in del_chunks:
                fsutil.fault("gc:chunk")
                s.delete_chunk(c)
                step()
            for p in tmp_files:
                fsutil.fault("gc:tmp")
                try:
                    fsutil.remove_quietly(p)
                except OSError as e:
                    logger.warning("一時ファイルを削除できませんでした(%s): %s", e, p.name)
                step()
        finally:
            # 版ファイルを消したので、履歴を読み直す
            self._history = History(self.bvc_dir)
            self._history.load()
        report.changed = True
        self._history.log_op(
            {**_op_entry("gc", {"no_git": no_git}, head),
             "deleted": {"commits": report.deleted_commits, "manifests": report.deleted_manifests,
                         "chunks": report.deleted_chunks, "tmp": report.deleted_tmp},
             "skipped": skipped}
        )
        return report

    def _tmp_files(self) -> list[Path]:
        # tmp/ の残骸(ロック中なので、書き込み途中のものは無い)。
        tmp = self.bvc_dir / "tmp"
        try:
            with os.scandir(fsutil.os_path(tmp)) as it:
                return sorted(tmp / e.name for e in it if e.is_file(follow_symlinks=False))
        except FileNotFoundError:
            return []
