# 各操作の手順(トランザクションの組み立て)。設計書 1.1節・3.7節。

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from .chunkers import make_chunker
from .codecs import POLICIES
from .errors import BrokenVersion, BvcError, CannotMove, MissingFiles, RevisionError, UsageError
from .fsutil import FileLock, atomic_write_json, compile_glob, load_json
from .history import History
from .model import (
    BranchInfo,
    CommitResult,
    Config,
    DiscardResult,
    GcReport,
    Head,
    LogEntry,
    MoveResult,
    Note,
    VerifyReport,
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
    ) -> MoveResult:
        # 移動系の共通手順(設計書 4.1節)。移動先は呼び出し側が自動コミットの前に決めておく。
        # branch が None なら、移動後もそのときの HEAD.branch(自動コミットがあればそのブランチ)を保つ。
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

    # --- M4-A: 履歴操作 ---

    def note(self, text: str, rev: str = "@") -> Note:
        # コメントを追記する(M4-1)。書き込みはhistory が原子的に行う。
        commit_id = self.resolve(rev)
        if not self._history.is_readable(commit_id):
            raise RevisionError(f"版 {commit_id} は読み込めません")
        return self._history.add_note(commit_id, text)

    def branches(self) -> list[BranchInfo]:
        # ブランチ一覧を返す(M4-2)。
        h = self._history
        # 生きているすべてのブランチを集める
        branches_set = set()
        for c in h.living():
            branches_set.add(c.branch)
        # tip から parent_tip を計算する
        result = []
        for branch_num in sorted(branches_set):
            tip = h.branch_tip(branch_num)
            if tip is not None:
                parent_commit = h.get(tip)
                parent_branch = parent_commit.branch if parent_commit else branch_num
                # 分岐の起点(別ブランチからの分岐)を探す
                parent_tip = None
                if parent_branch != branch_num:
                    parent_tip = h.branch_tip(parent_branch)
                result.append(
                    BranchInfo(
                        number=branch_num,
                        name=h.branch_name(branch_num),
                        tip=tip,
                        parent_tip=parent_tip,
                    )
                )
        return result

    def name_branch(self, name: str, rev: str = "@") -> BranchInfo:
        # ブランチに名前を付ける(M4-2)。
        commit_id = self.resolve(rev)
        c = self._history.get(commit_id)
        branch_num = c.branch
        self._history.name_branch(branch_num, name)
        # 結果を返す
        tip = self._history.branch_tip(branch_num)
        assert tip is not None
        return BranchInfo(
            number=branch_num,
            name=name,
            tip=tip,
            parent_tip=None,
        )

    def unname_branch(self, name: str) -> None:
        # ブランチ名を削除する(M4-2)。
        self._history.unname_branch(name)

    def discard(
        self, rev: str = "@", force: bool = False, allow_missing: bool = False
    ) -> DiscardResult:
        # 版に削除印を付ける(M4-3、設計書 4.11節)。
        # 現在位置なら自動コミット → 親へ復元する。
        with self._lock:
            h = self._history
            head = h.head()
            commit_id = self.resolve(rev)
            if h.is_discarded(commit_id):
                raise RevisionError(f"版 {commit_id} は既に削除済みです")

            # 削除対象が現在位置なら、親へ移動する
            if commit_id == head.at:
                parent = h.effective_parent(commit_id)
                if parent is None:
                    raise CannotMove(
                        "現在位置を削除できません(親がない版のため)",
                        details={"at": commit_id},
                    )
                # 親への移動時に自動コミット + 復元 を行う
                h.discard(commit_id)
                move_result = self._move_to_target(
                    target_id=parent,
                    op="discard",
                    arg=str(commit_id),
                    reason="",
                    allow_missing=allow_missing,
                )
                return DiscardResult(
                    changed=True,
                    before=move_result.before,
                    after=move_result.after,
                    auto_commit=move_result.auto_commit,
                    restored=move_result.restored,
                    deleted=move_result.deleted,
                )
            else:
                # 削除対象が現在位置でなければ、単に削除印を付けるだけ
                h.discard(commit_id)
                h.log_op({"op": "discard", "args": {"rev": rev}, "before": _head_json(head)})
                return DiscardResult(
                    changed=True,
                    before=head,
                    after=head,
                    auto_commit=None,
                    restored=[],
                    deleted=[],
                )

    def gc(self, dry_run: bool = False, no_git: bool = False, progress: ProgressFn = None) -> GcReport:
        # ガベージコレクション(M4-4、設計書 4.8節)。
        # mark: 生きている版 ∪ pin された版 → マニフェスト → チャンク
        # sweep: mark 外を削除
        with self._lock:
            h = self._history
            s = self._store

            # mark フェーズ: 保護するマニフェストの集合を計算する
            marked_manifests: set[str] = set()
            marked_commits: set[int] = set()

            # 1. 生きている版
            for c in h.living():
                marked_commits.add(c.id)
                marked_manifests.update(c.tree.values())

            # 2. pin された版(M6以降。これは簡略版)
            # TODO: pinned_ids() から tree を取得する

            # 3. git の全履歴から参照される版(M6)
            # TODO: gitlink が実装されたら git rev-list を呼ぶ

            # マニフェストからチャンク を計算する
            marked_chunks: set[str] = set()
            for manifest_sha in marked_manifests:
                try:
                    m = s.get_manifest(manifest_sha)
                    for ref in m.chunks:
                        marked_chunks.add(ref.sha)
                except Exception:
                    # 壊れたマニフェスト は無視(隔離されている可能性)
                    pass

            # sweep フェーズ: mark 外を削除
            if not dry_run:
                # 版を削除
                deleted_commits = []
                for cid in h.ids(include_discarded=True):
                    if cid not in marked_commits:
                        commit_file = h._bvc_dir / "commits" / f"{cid}.json"
                        if commit_file.exists():
                            commit_file.unlink()
                            deleted_commits.append(cid)
                            # notes も削除
                            notes_file = h._bvc_dir / "notes" / f"{cid}.jsonl"
                            if notes_file.exists():
                                notes_file.unlink()

                # マニフェストを削除
                deleted_manifests = 0
                for m_sha in s.iter_manifests():
                    if m_sha not in marked_manifests:
                        s.delete_manifest(m_sha)
                        deleted_manifests += 1

                # チャンク を削除
                deleted_chunks = 0
                for c_sha in s.iter_chunks():
                    if c_sha not in marked_chunks:
                        s.delete_chunk(c_sha)
                        deleted_chunks += 1

                # tmp の掃除
                tmp_dir = h._bvc_dir / "tmp"
                cleaned_tmp = 0
                if tmp_dir.exists():
                    for p in tmp_dir.glob("*"):
                        try:
                            p.unlink()
                            cleaned_tmp += 1
                        except Exception:
                            pass

                # freed_bytes は実装簡略版（実装が必要に応じて計算可能）
                freed_bytes = 0

                h.log_op({
                    "op": "gc",
                    "args": {"dry_run": False, "no_git": no_git},
                    "deleted_commits": deleted_commits,
                    "deleted_manifests": deleted_manifests,
                    "deleted_chunks": deleted_chunks,
                })

                return GcReport(
                    deleted_commits=deleted_commits,
                    deleted_manifests=deleted_manifests,
                    deleted_chunks=deleted_chunks,
                    cleaned_tmp=cleaned_tmp,
                    freed_bytes=freed_bytes,
                )
            else:
                # dry_run: 削除するもの の数を計算するだけ
                to_delete_commits = [
                    cid for cid in h.ids(include_discarded=True) if cid not in marked_commits
                ]
                to_delete_manifests = sum(1 for m_sha in s.iter_manifests() if m_sha not in marked_manifests)
                to_delete_chunks = sum(1 for c_sha in s.iter_chunks() if c_sha not in marked_chunks)
                return GcReport(
                    deleted_commits=to_delete_commits,
                    deleted_manifests=to_delete_manifests,
                    deleted_chunks=to_delete_chunks,
                    cleaned_tmp=0,
                    freed_bytes=0,
                )
