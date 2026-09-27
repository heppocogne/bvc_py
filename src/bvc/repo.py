# 各操作の手順(トランザクションの組み立て)。設計書 1.1節・3.7節。

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable, Final, NamedTuple

from . import fsutil, gitlink
from .chunkers import make_chunker
from .codecs import POLICIES
from .errors import (
    BrokenVersion,
    BvcError,
    CannotMove,
    CorruptData,
    FileBusy,
    IntegrityError,
    MissingFiles,
    PinnedCommit,
    RevisionError,
    SafetyAbort,
    UnsafePath,
    UnsupportedFormat,
    UsageError,
)
from .fsutil import BVC_DIR, FileLock, atomic_write_json, check_relpath, compile_glob, glob_match, load_json
from .gitlink import Git
from .history import History, check_branch_name
from .model import (
    BranchInfo,
    Commit,
    CommitResult,
    Config,
    DiscardResult,
    GcReport,
    GitConfig,
    Head,
    HooksResult,
    LockFile,
    LogEntry,
    Manifest,
    MoveResult,
    Note,
    PinResult,
    PostCheckoutResult,
    PreCommitResult,
    ProgressEvent,
    SyncResult,
    VerifyReport,
    WorkState,
)
from .store import HEALTH_KINDS, ObjectStore, ProgressFn
from .worktree import Worktree

logger = logging.getLogger(__name__)

# BVC_DIR は fsutil から import して再公開する(後方互換のため repo.BVC_DIR も使える)。
# {"name": "fixed", "size": "4M"}のような書き方も許容するため、Anyを使う
DEFAULT_CHUNKER: Final[dict[str, Any]] = {"name": "fixed", "size": 4194304}
_SUBDIRS: Final[tuple[str, ...]] = (
    "commits",
    "manifests",
    "chunks",
    "notes",
    "quarantine",
    "txn",
    "tmp",
)


def _config_error(msg: str) -> UsageError:
    return UsageError(f"config.json: {msg}")


def _missing_error(state: WorkState) -> MissingFiles:
    # 表示の形式は仕様書 2.6節
    lines = ["追跡ファイルが見つかりません"]
    for p in state.missing:
        lines.append(f"  missing: {p}")
        if state.hints.get(p):
            lines.append(
                f"  ヒント: パターン外に同じ内容のファイルがあります: {', '.join(state.hints[p])}"
            )
    lines.append("  削除として記録するには --allow-missing を指定してください")
    return MissingFiles(
        "\n".join(lines), missing=list(state.missing), hints=dict(state.hints)
    )


def _head_json(head: Head) -> dict:
    return {"at": head.at, "branch": head.branch}


def _op_entry(op: str, args: dict, head: Head) -> dict:
    # 現在位置を変えない操作の oplog の記録。
    return {
        "op": op,
        "args": args,
        "reason": "",
        "before": _head_json(head),
        "after": _head_json(head),
        "created": [],
        "result": "ok",
    }


class _LockView(NamedTuple):
    # 現在位置の版と bvc.lock・lockstate.json の状態(M6-2)。_update_lock / _check_lock で共有する。

    tree: dict[str, str]        # 現在位置の版の tree
    th: str                     # その tree_hash
    found: bool                 # lockstate.json の記録があるか
    saved: str | None           # 記録された tree_hash(None は bvc.lock が無い状態、または不明)
    lock: LockFile | None       # 作業ツリーの bvc.lock(無い・読めなければ None)
    err: BvcError | None        # 読めなかった理由
    raw: bytes | None           # 元のバイト列(無ければ None)

    @property
    def lh(self) -> str | None:
        # bvc.lock の内容の tree_hash(読めなければ None)。
        return gitlink.tree_hash(self.lock.tree) if self.lock is not None else None


def _fields_of(obj: Any) -> dict:
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


def _load_config(bvc_dir: Path) -> dict:
    # config.json は自動復旧しない(仕様書 2.9節)。無い・壊れている場合は、修正方法を案内して中止する。
    try:
        return load_json(bvc_dir / "config.json", "config.json")
    except (FileNotFoundError, CorruptData) as e:
        reason = "ファイルがありません" if isinstance(e, FileNotFoundError) else str(e)
        example = '{"format": 1, "track": ["*.bin"]}'
        raise BvcError(
            f"設定ファイルを読み込めません({reason})。config.json は自動では復旧しません。\n"
            f"  {bvc_dir / 'config.json'} を修正してください(最小の例: {example})"
        ) from e


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
    if (
        not isinstance(track, list)
        or not track
        or not all(type(p) is str and p for p in track)
    ):
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
            raise _config_error(
                f"{where} は {', '.join(POLICIES)} のいずれかにしてください: {c!r}"
            )
        return c

    chunker = check_chunker(data.get("chunker", DEFAULT_CHUNKER), "chunker")
    compression = check_compression(data.get("compression", "auto"), "compression")

    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise _config_error("rules はリストにしてください")
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if (
            not isinstance(r, dict)
            or type(r.get("pattern")) is not str
            or not r["pattern"]
        ):
            raise _config_error(f"{where} には pattern(文字列)が必要です")
        if "chunker" in r:
            check_chunker(r["chunker"], f"{where}.chunker")
        if "compression" in r:
            check_compression(r["compression"], f"{where}.compression")

    commit_verify = data.get("commit_verify", "exists")
    if commit_verify not in ("exists", "full"):
        raise _config_error(
            f"commit_verify は exists か full にしてください: {commit_verify!r}"
        )
    threads = data.get("threads", 0)
    if type(threads) is not int or threads < 0:
        raise _config_error("threads は 0 以上の整数にしてください")
    rename_threshold = data.get("rename_threshold", 0.5)
    if type(rename_threshold) not in (int, float) or not 0 < rename_threshold <= 1:
        raise _config_error("rename_threshold は 0 より大きく 1 以下の数にしてください")

    for p in track + ignore + [r["pattern"] for r in rules]:
        compile_glob(p)
    git = _parse_git_config(data.get("git", {}), track, ignore)
    return Config(
        track=list(track),
        ignore=list(ignore),
        rules=list(rules),
        chunker=chunker,
        compression=compression,
        commit_verify=commit_verify,
        rename_threshold=float(rename_threshold),
        threads=threads,
        git=git,
    )


def _parse_git_config(git: Any, track: list[str], ignore: list[str]) -> GitConfig:
    # config.json の git(仕様書 4節)。bvc.lock は追跡対象にできない(バイナリとして記録されてしまうため)。
    if not isinstance(git, dict):
        raise _config_error("git は辞書にしてください")
    enabled = git.get("enabled", False)
    if type(enabled) is not bool:
        raise _config_error("git.enabled は true か false にしてください")
    lock_file = git.get("lock_file", "bvc.lock")
    try:
        if check_relpath(lock_file) != lock_file:
            raise UnsafePath("")
    except UnsafePath:
        raise _config_error(f"git.lock_file に使えないパスです: {lock_file!r:.80}") from None
    if (
        enabled
        and any(glob_match(p, lock_file) for p in track)
        and not any(glob_match(p, lock_file) for p in ignore)
    ):
        raise _config_error(
            f"git.lock_file({lock_file})が追跡パターンに一致します。ignore に加えるか、追跡パターンを変えてください"
        )
    pre_commit = git.get("pre_commit", "snapshot")
    if pre_commit not in ("snapshot", "reject"):
        raise _config_error(f"git.pre_commit は snapshot か reject にしてください: {pre_commit!r}")
    return GitConfig(enabled=enabled, lock_file=lock_file, pre_commit=pre_commit)


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
        # git 連携(M6)。lock_status は開いたときの bvc.lock の状態(ok / out_of_sync / missing / disabled)
        self._git = Git(workdir) if config.git.enabled else None
        self.lock_status = "ok" if config.git.enabled else "disabled"

    # --- 開く・閉じる ---

    @staticmethod
    def find_workdir(start: Path) -> Path | None:
        # start から上位へ .bvc を探し、それを含むフォルダを返す(M2-2)。
        current = fsutil.real_path(start)
        while True:
            if fsutil.is_dir(current / BVC_DIR):
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
        progress: ProgressFn | None = None,
    ) -> Repo:
        # 新しいリポジトリを作り、その時点の追跡ファイルを版 0(kind=init)として記録する(M2-10)。
        # git なら git 連携を有効にし、bvc.lock を作る(M6-3)。フックの設置は呼び出し側(install_hooks)。
        workdir = fsutil.real_path(workdir)
        if not fsutil.is_dir(workdir):
            raise BvcError(f"作業フォルダがありません: {workdir}")
        # gear の seed を自動生成(M7-1)
        ck = chunker or dict(DEFAULT_CHUNKER)
        if ck.get("name") == "gear" and "seed" not in ck:
            ck = dict(ck)
            ck["seed"] = secrets.randbits(64)
        config_data = {
            "format": 1,
            "track": list(track),
            "ignore": list(ignore or []),
            "rules": [],
            "chunker": ck,
            "compression": compression,
            "commit_verify": "exists",
            "rename_threshold": 0.5,
            "threads": 0,
        }
        if git:
            config_data["git"] = {"enabled": True, "lock_file": "bvc.lock", "pre_commit": "snapshot"}
        config = parse_config(config_data)
        found = cls.find_workdir(workdir)
        if found is not None:
            raise BvcError(f"すでにリポジトリがあります: {found / BVC_DIR}")
        if git and not Git(workdir).is_work_tree():
            raise BvcError(
                f"git の作業ツリーの中ではないため、git 連携を有効にできません: {workdir}\n"
                "  先に git init を実行してください(git が無い場合はインストールしてください)"
            )

        bvc_dir = workdir / BVC_DIR
        os.mkdir(fsutil.os_path(bvc_dir))
        # 作ったばかりの .bvc だけを片付けの対象にする(作業ファイルには触れない)
        lock = FileLock(bvc_dir / "lock")
        repo: Repo | None = None
        try:
            lock.acquire()
            for d in _SUBDIRS:
                os.mkdir(fsutil.os_path(bvc_dir / d))
            tmp = bvc_dir / "tmp"
            atomic_write_json(bvc_dir / "config.json", config_data, tmp)
            atomic_write_json(
                bvc_dir / "counters.json",
                {"format": 1, "next_commit": 0, "next_branch": 0},
                tmp,
            )
            atomic_write_json(
                bvc_dir / "branches.json", {"format": 1, "names": {}}, tmp
            )
            atomic_write_json(
                bvc_dir / "health.json",
                {"format": 1, "bad_chunks": {}, "bad_manifests": {}, "bad_commits": {}},
                tmp,
            )
            repo = cls(workdir, config, lock)
            repo._history.load()
            state = repo._worktree.state(base_tree={}, store_chunks=True, progress=progress)
            if not state.tree:
                logger.warning(
                    "追跡対象のファイルがありません(パターン: %s)",
                    ", ".join(config.track),
                )
            commit = repo._history.new_commit(
                parent=None,
                tree=state.tree,
                kind="init",
                message="",
                stats={"new_bytes": state.new_bytes, "total_bytes": state.total_bytes},
            )
            head = Head(at=commit.id, branch=commit.branch)
            repo._history.set_head(head)
            repo._update_lock(head)
            repo._worktree.update_index(state.tree, state.fs_time_ns)
            repo._history.log_op(
                {
                    "op": "init",
                    "args": {},
                    "before": None,
                    "after": {"at": commit.id, "branch": commit.branch},
                    "created": [commit.id],
                    "result": "ok",
                }
            )
        except BaseException:
            if repo is not None:
                repo._store.close()
            lock.release()
            shutil.rmtree(fsutil.os_path(bvc_dir), ignore_errors=True)
            raise
        return repo

    @classmethod
    def open(cls, start: Path, warn_out_of_sync: bool = True) -> Repo:
        # start から上位へ .bvc を探して開く(M2-10)。ロック取得 → recover → History.load。
        workdir = cls.find_workdir(start)
        if workdir is None:
            raise BvcError(
                f"リポジトリが見つかりません({fsutil.real_path(start)} とその上位に {BVC_DIR} がありません)"
            )
        bvc_dir = workdir / BVC_DIR
        lock = FileLock(bvc_dir / "lock")
        lock.acquire()
        repo: Repo | None = None
        try:
            try:
                config = parse_config(_load_config(bvc_dir))
                repo = cls(workdir, config, lock)
                repo._worktree.recover(repo._history.set_head)
                repo._recover_control()
                repo._check_lock(warn_out_of_sync)
            except OSError as e:
                # 読み込み自体の失敗(使用中など)は破損の証拠ではないので、何も変えずに中止する(D-15、I-20)
                raise FileBusy(
                    f"管理ファイルを読み込めません(他のアプリが使用中の可能性があります): {e}"
                ) from e
        except BaseException:
            if repo is not None:
                repo._store.close()
            lock.release()
            raise
        return repo

    def _recover_control(self) -> None:
        # 管理ファイルを検査し、無い・解析できない・形式が不正なものを自動で作り直す(M4-10、設計書 4.12節)。
        # 作業ファイルは変えない。作り直したら警告を出し、oplog に recover_control を記録する。
        h, wt = self._history, self._worktree
        recovered: list[dict] = []

        def note(file: str, problem: str, action: str) -> None:
            logger.warning("%s を自動で復旧しました: %s(%s)", file, action, problem)
            recovered.append({"file": file, "problem": problem, "action": action})

        # branches.json は load の中で読むので、先に直しておく
        problem = h.check_branches()
        if problem is not None:
            h.reset_branches()
            note("branches.json", problem, "ブランチ名を失ったため、空で作り直しました")
        h.load()

        problem = h.check_counters()
        if problem is not None:
            c = h.rebuild_counters()
            note(
                "counters.json",
                problem,
                f"版・削除印・操作ログから再計算しました(次の版 {c['next_commit']}、次のブランチ {c['next_branch']})",
            )

        problem = h.check_head()
        if problem is not None:
            head, how = self._guess_head()
            h.set_head(head)
            note("HEAD.json", problem, f"{how}から現在位置を版 {head.at} にしました")

        problem = wt.check_index()
        if problem is not None:
            wt.reset_index()
            note("index.json", problem, "空で作り直しました(次の操作で全追跡ファイルをハッシュし直します)")

        health = self._store.health
        if health.problem is not None:
            problem = {"missing": "ファイルがありません"}.get(health.problem, health.problem)
            health.rebuild()
            note("health.json", problem, "空で作り直しました(壊れたデータは次の verify で再検出されます)")

        if recovered:
            head = h.head()
            for r in recovered:
                h.log_op(
                    {
                        "op": "recover_control",
                        "args": r,
                        "reason": "",
                        "before": None,
                        "after": _head_json(head),
                        "created": [],
                        "result": "ok",
                    }
                )

    def _guess_head(self) -> tuple[Head, str]:
        # HEAD.json が使えないときの現在位置(設計書 4.12節): 操作ログの最後の after →
        # 作業ファイルと内容が一致する版 → 最新の版、の順に決める。作業ファイルは変えない。
        h = self._history
        head = h.head_from_oplog()
        if head is not None:
            return head, "操作ログ"
        living = [c for c in h.living() if h.tree_known(c.id)]
        if not living:
            raise BvcError(
                f"HEAD.json を復旧できません(読み込める版がありません)。{BVC_DIR}/commits を確認してください"
            )
        tree = self._worktree.state(base_tree={}, store_chunks=False, find_hints=False).tree
        for c in living:  # 新しい順
            if c.tree == tree:
                return Head(c.id, c.branch), "作業ファイルと内容が一致する版"
        c = living[0]
        logger.warning(
            "作業ファイルと内容が一致する版が見つからないため、最新の版 %d を現在位置にしました。"
            "作業ファイルは変えていません(未コミットの変更として扱われます)",
            c.id,
        )
        return Head(c.id, c.branch), "最新の版"

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
        return self._worktree.state(
            base_tree=self._history.get(head.at).tree, store_chunks=False
        )

    @staticmethod
    def _normalize_renames(
        renames: list[tuple[str, str]] | None,
    ) -> list[tuple[str, str]]:
        # 名前変更の手動指定を、記録と同じ形式('/' 区切り、NFC)の相対パスにする。
        # 作業フォルダからの相対パスとして扱い、'\\' も区切りとみなす。
        out = []
        for pair in renames or ():
            norm = []
            for p in pair:
                q = p.replace("\\", "/")
                while q.startswith("./"):
                    q = q[2:]
                try:
                    norm.append(check_relpath(q))
                except UnsafePath as e:
                    raise UsageError(
                        f"--rename: 使えないパスです: {p}({e})", path=p
                    ) from None
            out.append((norm[0], norm[1]))
        return out

    def commit(
        self,
        message: str = "",
        allow_missing: bool = False,
        renames: list[tuple[str, str]] | None = None,
        kind: str = "commit",
        progress: ProgressFn | None = None,
    ) -> CommitResult:
        # 追跡ファイルの現状を新しい版として記録する(M2-10)。
        # 書き込み順: チャンク → マニフェスト → counters → 版 → HEAD → bvc.lock → index → oplog(設計書 4.7節)。
        renames = self._normalize_renames(renames)
        head = self._history.head()
        base = self._history.get(head.at)
        state = self._worktree.state(
            base_tree=base.tree,
            store_chunks=True,
            renames=renames,
            find_hints=not allow_missing,
            progress=progress,
        )

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
        self._update_lock(after)
        self._worktree.update_index(state.tree, state.fs_time_ns)
        self._history.log_op(
            {
                "op": kind,
                "args": {"message": message, "allow_missing": allow_missing},
                "before": _head_json(head),
                "after": _head_json(after),
                "created": [commit.id],
                "result": "ok",
            }
        )
        return CommitResult(
            changed=True, commit=commit, state=state, new_branch=new_branch
        )

    # --- 移動系(M3-6, M3-7、設計書 4.1節・4.5節) ---

    def undo(
        self,
        reason: str = "",
        allow_missing: bool = False,
        skip_broken: bool = False,
        progress: ProgressFn | None = None,
    ) -> MoveResult:
        # 親の版(effective_parent)へ移動する。現在のブランチ(redo の方向)は変えない。
        # skip_broken なら、壊れた版を飛ばして親の方向で最も近い健全な版へ移動する(設計書 4.11節)。
        h = self._history
        head = h.head()
        target = h.effective_parent(head.at)
        if target is None:
            raise CannotMove(
                f"版 {head.at} は根(親の無い版)なので、これ以上戻れません", at=head.at
            )
        target, skipped = self._skip_broken("undo", target, skip_broken, h.effective_parent)
        return self._move(
            "undo", "", reason, target, None, allow_missing, progress, skipped=skipped
        )

    def redo(
        self,
        reason: str = "",
        allow_missing: bool = False,
        skip_broken: bool = False,
        progress: ProgressFn | None = None,
    ) -> MoveResult:
        # 現在のブランチの先端へ向かって1つ進む。経路上に無ければ、子が1つのときだけ進む。
        # skip_broken なら、壊れた版を飛ばして同じ方向で最も近い健全な版へ移動する(設計書 4.11節)。
        h = self._history
        head = h.head()
        path = h.path_to_tip(head.branch)

        def forward(cur: int) -> int | None:
            # cur から先端方向へ1つ進んだ版(無ければ None)。子が複数で決められなければ CannotMove。
            if cur in path:
                idx = path.index(cur)
                return path[idx - 1] if idx > 0 else None
            kids = h.children(cur)
            if len(kids) > 1:
                raise CannotMove(
                    f"版 {cur} には子が複数あるため、進む先を決められません。"
                    f"goto で版を指定してください(候補: {', '.join(map(str, kids))})",
                    at=cur,
                    candidates=kids,
                )
            return kids[0] if kids else None

        target = forward(head.at)
        if target is None:
            raise CannotMove(
                f"版 {head.at} はブランチの先端(子の無い版)なので、これ以上進めません",
                at=head.at,
            )
        target, skipped = self._skip_broken("redo", target, skip_broken, forward)
        # 経路上ならブランチはそのまま、経路の外なら移動先の版のブランチにする
        branch = head.branch if target in path else h.get(target).branch
        return self._move(
            "redo", "", reason, target, branch, allow_missing, progress, skipped=skipped
        )

    def _skip_broken(
        self,
        op: str,
        target: int,
        skip_broken: bool,
        step: Callable[[int], int | None],
    ) -> tuple[int, list[int]]:
        # 移動先が壊れた版なら、skip_broken でなければ BrokenVersion で中止する(何も変えない)。
        # skip_broken なら step で同じ方向へたどり、最初の健全な版と、飛ばした版の一覧を返す。
        skipped: list[int] = []
        cur: int | None = target
        while cur is not None and not self._is_healthy(cur):
            if not skip_broken:
                raise BrokenVersion(
                    f"移動先の版 {cur} は壊れているため移動できません(作業ファイルは変えていません)。"
                    f"壊れた版を飛ばすには {op} --skip-broken を指定してください",
                    commit=cur,
                )
            skipped.append(cur)
            try:
                cur = step(cur)
            except CannotMove as e:
                raise BrokenVersion(
                    f"版 {', '.join(map(str, skipped))} は壊れています。{e}", commit=skipped[0]
                ) from None
        if cur is None:
            raise BrokenVersion(
                f"{'親' if op == 'undo' else '先端'}の方向に健全な版がありません"
                f"(壊れた版: {', '.join(map(str, skipped))})。作業ファイルは変えていません",
                commit=skipped[0],
            )
        return cur, skipped

    def _is_healthy(self, commit_id: int) -> bool:
        # 版を復元できる見込みがあるか(版ファイルが読めて tree が正しく、全マニフェストとチャンクが揃っている)。
        # チャンクの中身までは読まない(展開時に照合する)。見つかった異常は隔離して記録される。
        h = self._history
        if not h.exists(commit_id) or h.is_broken(commit_id):
            return False
        return all(
            self._store.manifest_ok(sha, "exists")
            for sha in sorted(set(h.get(commit_id).tree.values()))
        )

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
                {
                    "op": "goto",
                    "args": {"rev": rev},
                    "reason": "",
                    "before": _head_json(head),
                    "after": _head_json(after),
                    "created": [],
                    "result": "ok",
                }
            )
            return MoveResult(changed=True, before=head, after=after)
        return self._move(
            "goto", rev, "", target, branch, allow_missing, progress, {"rev": rev}
        )

    def _check_movable(self, target: int) -> None:
        # 移動先の版が読めて、tree に不正な値が無いか(マニフェスト・チャンクは restore の事前検査で確かめる)。
        if not self._history.exists(target):
            raise RevisionError(f"版 {target} は存在しません")
        if self._history.is_broken(target):
            raise BrokenVersion(
                f"版 {target} は壊れているため移動できません", commit=target
            )

    def _move(
        self,
        op: str,
        arg: str,
        reason: str,
        target_id: int | None,
        branch: int | None,
        allow_missing: bool,
        progress: ProgressFn | None,
        args: dict | None = None,
        on_done: Callable[[], None] | None = None,
        skipped: list[int] | None = None,
        import_tree: dict[str, str] | None = None,
    ) -> MoveResult:
        # 移動系の共通手順(設計書 4.1節)。移動先は呼び出し側が自動コミットの前に決めておく。
        # branch が None なら、移動後もそのときの HEAD.branch(自動コミットがあればそのブランチ)を保つ。
        # on_done は復元が成功した後、oplog の前に呼び出す(discard の削除印など)。
        # import_tree を渡すと(target_id は None)、自動コミットの後にその内容の版(kind=import)を
        # 自動コミット(無ければ現在位置)の子として作り、そこへ移動する(sync、M6-4)。
        h, wt = self._history, self._worktree
        head = h.head()
        if import_tree is None:
            self._check_movable(target_id)
            target_tree = h.get(target_id).tree
        else:
            target_tree = import_tree
        base = h.get(head.at)

        # 事前検査(衝突・パス・保存データ)。ここまでは何も変えない
        files = wt.check_target(target_tree)
        paths = set(base.tree) | set(target_tree) | set(files)
        # 上書き・削除し得るパスは、stat キャッシュを使わずにハッシュする(仕様書 2.8節)
        touched = {
            p
            for p in paths
            if base.tree.get(p) != target_tree.get(p) or p not in target_tree
        }
        state = wt.state(
            base_tree=base.tree,
            store_chunks=True,
            no_cache=touched,
            find_hints=not allow_missing,
            progress=progress,
        )
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
            # sync では bvc.lock が合わせる先なので、自動コミットの内容で上書きしない
            # (復元が失敗すれば、bvc.lock と HEAD が食い違ったまま=非同期状態として残る)
            if op != "sync":
                self._update_lock(current)
            wt.update_index(state.tree, state.fs_time_ns)

        created = [auto.id] if auto is not None else []
        imported = None
        if import_tree is not None:
            imported = h.new_commit(
                parent=current.at,
                tree=import_tree,
                kind="import",
                message="sync: bvc.lock から作成",
                stats={"new_bytes": 0, "total_bytes": self._tree_size(import_tree)},
            )
            created.append(imported.id)
            target_id, branch = imported.id, imported.branch

        after = Head(target_id, current.branch if branch is None else branch)

        def on_committed(hd: Head) -> None:
            h.set_head(hd)
            self._update_lock(hd)

        entry = {
            "op": op,
            "args": {
                **(args or {}),
                "allow_missing": allow_missing,
                **({"skipped": skipped} if skipped else {}),
            },
            "reason": reason,
            "before": _head_json(head),
            "created": created,
        }
        try:
            res = wt.restore(target_tree, state, after, on_committed, progress)
        except BaseException as e:
            if created:
                # 自動コミットで履歴は変わったので、中止したことも記録する
                try:
                    h.log_op(
                        {
                            **entry,
                            "after": _head_json(current),
                            "result": "error",
                            "error": str(e),
                        }
                    )
                except Exception:
                    logger.warning("操作ログに記録できませんでした", exc_info=True)
            raise
        if on_done is not None:
            on_done()
        h.log_op({**entry, "after": _head_json(after), "result": "ok"})
        result = MoveResult(
            changed=True,
            before=head,
            after=after,
            auto_commit=auto,
            restored=res.written,
            deleted=res.deleted,
            skipped=list(skipped or []),
        )
        if import_tree is not None:
            return SyncResult(**_fields_of(result), imported=imported)
        return result

    def resolve(self, rev: str) -> int:
        # リビジョン式を版番号にする。
        return self._history.resolve(rev, self._history.head())

    def log(
        self, include_discarded: bool = False, limit: int | None = None
    ) -> list[LogEntry]:
        # 版を新しい順に返す(M2-10)。読めない版も含める(commit=None)。
        h = self._history
        head = h.head()
        pinned = h.pinned_ids()
        data_broken = self._recorded_broken()
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
                    broken=h.is_broken(cid) or (c is not None and data_broken(c)),
                    discarded=h.is_discarded(cid),
                    pinned=cid in pinned,
                    notes=h.get_notes(cid),
                )
            )
        if limit is not None:
            entries = entries[:limit]
        return entries

    def _recorded_broken(self) -> Callable[[Commit], bool]:
        # 健全性の記録(health)から、版が壊れたデータを参照しているかを判定する関数を返す(M4-11)。
        # 表示用なので、マニフェストは隔離も記録もせずに読む(記録されていない異常は verify で見つける)。
        store = self._store
        bad_manifests = set(store.health.records("bad_manifests"))
        bad_chunks = set(store.health.records("bad_chunks"))
        cache: dict[str, bool] = {}

        def manifest_bad(sha: str) -> bool:
            if sha not in cache:
                if sha in bad_manifests:
                    cache[sha] = True
                elif not bad_chunks:
                    cache[sha] = False
                else:
                    m = store.peek_manifest(sha)
                    cache[sha] = m is not None and any(r.sha in bad_chunks for r in m.chunks)
            return cache[sha]

        def broken(c: Commit) -> bool:
            if not bad_manifests and not bad_chunks:
                return False
            return any(manifest_bad(sha) for sha in set(c.tree.values()))

        return broken

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
            number=branch,
            name=h.branch_name(branch),
            tip=tip,
            fork=fork,
            is_current=branch == head.branch,
        )

    def name_branch(self, name: str, rev: str = "@") -> BranchInfo:
        # rev が属するブランチに名前を付ける(M4-2)。同じ名前が別のブランチにあれば付け替える。
        _check_name(name)
        h = self._history
        head = h.head()
        commit_id = h.resolve(rev, head)
        if not h.is_readable(commit_id):
            raise BvcError(
                f"版 {commit_id} は読み込めないため、属するブランチが分かりません"
            )
        branch = h.get(commit_id).branch
        previous = h.name_branch(branch, name)
        if previous is not None:
            tip = h.branch_tip(previous)
            logger.info(
                "名前 '%s' を別のブランチ(先端 %s)から付け替えました",
                name,
                "なし" if tip is None else tip,
            )
        h.log_op(
            _op_entry(
                "branch_name",
                {"name": name, "rev": rev, "branch": branch, "previous": previous},
                head,
            )
        )
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
            h.log_op(
                _op_entry("discard", {**args, "allow_missing": allow_missing}, head)
            )
            return DiscardResult(
                changed=True, discarded=commit_id, before=head, after=head
            )

        parent = h.effective_parent(commit_id)
        if parent is None:
            raise CannotMove(
                f"版 {commit_id} は根(親の無い版)なので、現在位置のまま削除できません。別の版へ移動してから削除してください",
                at=commit_id,
            )
        res = self._move(
            "discard",
            "" if rev == "@" else rev,
            "",
            parent,
            None,
            allow_missing,
            progress,
            args,
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

    def gc(
        self,
        dry_run: bool = False,
        no_git: bool = False,
        progress: ProgressFn | None = None,
    ) -> GcReport:
        # 削除済みの版と、参照されないマニフェスト・チャンク・一時ファイルを消す(M4-4、設計書 4.8節)。
        # git 連携が有効なら、git の全履歴にある bvc.lock が参照するデータを残す(no_git なら履歴は見ない)。
        # 削除順は 版 → マニフェスト → チャンク → 一時ファイル。途中で止まっても、残るのは参照されないものだけ(R-8)。
        # 参照先を把握できない版・マニフェストがあれば、その先の削除は見送る(D-15)。
        h, s = self._history, self._store
        head = h.head()
        pinned = h.pinned_ids()
        ids = h.ids(include_discarded=True)
        garbage = [
            c for c in ids if h.is_discarded(c) and c not in pinned and c != head.at
        ]
        kept = [c for c in ids if c not in set(garbage)]

        # mark(git 連携が有効なら、bvc.lock から参照されるマニフェストも残す。M6-5)
        lock_manifests = self._lock_manifests(no_git)
        unknown = [c for c in kept if not h.tree_known(c)]
        manifests: set[str] = set()
        for c in kept:
            if h.tree_known(c):
                manifests.update(h.get(c).tree.values())
        # 既に無いマニフェストは守る必要が無い(読めないものとして扱うと、チャンクの削除まで止まるため)
        manifests.update(
            m for m in lock_manifests if os.path.exists(fsutil.os_path(s.manifest_path(m)))
        )
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
        del_manifests = (
            []
            if "manifests" in skipped
            else [m for m in s.iter_manifests() if m not in manifests]
        )
        del_chunks = (
            []
            if "chunks" in skipped
            else [c for c in s.iter_chunks() if c not in chunks]
        )
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
        total = (
            len(garbage)
            + len(orphan_notes)
            + len(del_manifests)
            + len(del_chunks)
            + len(tmp_files)
        )
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
                    logger.warning(
                        "一時ファイルを削除できませんでした(%s): %s", e, p.name
                    )
                step()
        finally:
            # 版ファイルを消したので、履歴を読み直す
            self._history = History(self.bvc_dir)
            self._history.load()
        report.changed = True
        self._history.log_op(
            {
                **_op_entry("gc", {"no_git": no_git}, head),
                "deleted": {
                    "commits": report.deleted_commits,
                    "manifests": report.deleted_manifests,
                    "chunks": report.deleted_chunks,
                    "tmp": report.deleted_tmp,
                },
                "skipped": skipped,
            }
        )
        return report

    # --- 検証と修復(M4-9、仕様書 3.10節、設計書 4.9節) ---

    def verify(
        self,
        quick: bool = False,
        repair: bool = False,
        progress: ProgressFn | None = None,
    ) -> VerifyReport:
        # 全チャンク・全マニフェストを検証し、生きている版(と pin された版)が復元できるかを調べる。
        # 見つけた異常は隔離し、health.json に記録する。repair なら、壊れたチャンク・マニフェストを
        # 作業フォルダのファイルから作り直す。異常が残っていても例外にはせず、report.ok で返す。
        h, s = self._history, self._store
        before = {k: s.health.records(k) for k in HEALTH_KINDS}
        res = s.verify_all(quick=quick, progress=progress)
        report = VerifyReport(
            changed=False,
            quick=quick,
            repair=repair,
            checked_chunks=res.checked_chunks,
            checked_manifests=res.checked_manifests,
        )
        pinned = h.pinned_ids()
        kept = [c for c in h.ids(include_discarded=True) if not h.is_discarded(c) or c in pinned]
        report.checked_commits = len(kept)

        # 版ファイルの状態を記録する(読めるようになったものは記録から消す)
        for cid in h.ids(include_discarded=True):
            key = str(cid)
            if h.tree_known(cid):
                s.health.clear("bad_commits", key)
            elif not s.health.is_bad("bad_commits", key):
                reason = "unreadable" if not h.is_readable(cid) else "invalid_tree"
                s.health.mark("bad_commits", key, reason)

        broken, bad_chunks, bad_manifests, manifests = self._check_versions(kept)
        if repair and (bad_chunks or bad_manifests):
            chunks_done, manifests_done = self._repair(
                kept, bad_chunks, bad_manifests, manifests, progress
            )
            report.repaired_chunks = sorted(chunks_done)
            report.repaired_manifests = sorted(manifests_done)
            if chunks_done or manifests_done:
                broken, bad_chunks, bad_manifests, _ = self._check_versions(kept)
        report.broken_commits = sorted(broken)
        report.bad_chunks = sorted(bad_chunks)
        report.bad_manifests = sorted(bad_manifests)
        report.changed = bool(
            report.repaired_chunks
            or report.repaired_manifests
            or before != {k: s.health.records(k) for k in HEALTH_KINDS}
        )
        h.log_op(
            {
                **_op_entry("verify", {"quick": quick, "repair": repair}, h.head()),
                "broken": report.broken_commits,
                "repaired": {
                    "chunks": len(report.repaired_chunks),
                    "manifests": len(report.repaired_manifests),
                },
            }
        )
        return report

    def _check_versions(
        self, ids: list[int]
    ) -> tuple[set[int], set[str], set[str], dict[str, Manifest]]:
        # 版を復元できるかを調べる。(壊れた版, 欠損・破損チャンク, 欠損・破損マニフェスト,
        # 読めたマニフェスト) を返す。チャンク・マニフェストは ids の版から参照されるものだけ。
        # 記録されていない欠損は、get_manifest が隔離記録する(チャンクの欠損は verify_all が記録済み)。
        h, s = self._history, self._store
        broken: set[int] = set()
        bad_chunks: set[str] = set()
        bad_manifests: set[str] = set()
        manifests: dict[str, Manifest] = {}
        chunk_ok: dict[str, bool] = {}
        for cid in ids:
            if not h.tree_known(cid):
                broken.add(cid)
                continue
            for sha in sorted(set(h.get(cid).tree.values())):
                if sha not in manifests and sha not in bad_manifests:
                    try:
                        manifests[sha] = s.get_manifest(sha)
                    except CorruptData:
                        bad_manifests.add(sha)
                if sha in bad_manifests:
                    broken.add(cid)
                    continue
                for ref in manifests[sha].chunks:
                    if ref.sha not in chunk_ok:
                        chunk_ok[ref.sha] = s.has_chunk(ref.sha, "exists")
                    if not chunk_ok[ref.sha]:
                        bad_chunks.add(ref.sha)
                        broken.add(cid)
        return broken, bad_chunks, bad_manifests, manifests

    def _repair(
        self,
        ids: list[int],
        bad_chunks: set[str],
        bad_manifests: set[str],
        manifests: dict[str, Manifest],
        progress: ProgressFn | None,
    ) -> tuple[set[str], set[str]]:
        # 作業フォルダのファイルを分割し、壊れたチャンク・マニフェストと同じものがあれば保存し直す。
        # 材料: 追跡ファイルと、追跡対象外で「壊れた版に記録されたファイル名」か「そのファイルのサイズ」が
        # 一致するファイル。分割方式は、設定のもの(rules を含む)と、読めたマニフェストに記録されたもの。
        h, s = self._history, self._store
        want_chunks, want_manifests = set(bad_chunks), set(bad_manifests)
        names: set[str] = set()
        sizes: set[int] = set()
        for cid in ids:
            if not h.tree_known(cid):
                continue
            for path, sha in h.get(cid).tree.items():
                m = manifests.get(sha)
                if sha in want_manifests or (
                    m is not None and any(r.sha in want_chunks for r in m.chunks)
                ):
                    names.add(path.rpartition("/")[2])
                    if m is not None:
                        sizes.add(m.size)
        specs: dict[bytes, dict] = {}
        rule_chunkers = [r["chunker"] for r in self.config.rules if "chunker" in r]
        for ck in [self.config.chunker, *rule_chunkers, *(m.chunker for m in manifests.values())]:
            specs.setdefault(fsutil.canonical_json(ck), ck)

        done_chunks: set[str] = set()
        done_manifests: set[str] = set()
        files = self._worktree.repair_candidates(names, sizes)
        for i, rel in enumerate(files, 1):
            if not want_chunks and not want_manifests:
                break
            try:
                with open(fsutil.os_path(self.workdir / rel), "rb") as f:
                    for spec in specs.values():
                        try:
                            got_c, got_m = s.repair_from(f, spec, want_chunks, want_manifests)
                        except ValueError:
                            continue  # 記録された分割方式が使えない(不明な名前など)
                        finally:
                            f.seek(0)
                        done_chunks |= got_c
                        done_manifests |= got_m
                        want_chunks -= got_c
                        want_manifests -= got_m
                        if not want_chunks and not want_manifests:
                            break
            except OSError as e:
                logger.warning("修復の材料にできませんでした(%s): %s", e, rel)
            if progress is not None:
                progress(ProgressEvent("repair", i, len(files), rel))
        return done_chunks, done_manifests

    # --- git 連携(M6、仕様書 3.11節・3.12節・5節、設計書 5節) ---

    @property
    def git_enabled(self) -> bool:
        return self._git is not None

    def _require_git(self) -> Git:
        if self._git is None:
            raise BvcError(
                "git 連携が無効です(config.json の git.enabled が false)。"
                "有効にするには config.json に \"git\": {\"enabled\": true} を書いてください"
            )
        return self._git

    @property
    def _lock_path(self) -> Path:
        return self.workdir / self.config.git.lock_file

    def _tree_size(self, tree: dict[str, str]) -> int:
        total = 0
        for sha in set(tree.values()):
            m = self._store.peek_manifest(sha)
            total += m.size if m is not None else 0
        return total

    def _read_work_lock(self) -> tuple[LockFile | None, BvcError | None, bytes | None]:
        # 作業ツリーの bvc.lock を読む。(内容, 読めなかった理由, 元のバイト列)。無ければ (None, None, None)。
        raw = gitlink.read_lock_raw(self._lock_path)
        if raw is None:
            return None, None, None
        try:
            return gitlink.parse_lock(raw, self.config.git.lock_file), None, raw
        except (IntegrityError, UnsupportedFormat) as e:
            return None, e, raw

    def _lock_view(self, head: Head) -> _LockView | None:
        # 現在位置の版と bvc.lock・lockstate の状態を1回だけ読む。版の tree が分からなければ None。
        h = self._history
        if not h.tree_known(head.at):
            return None
        tree = h.get(head.at).tree
        found, saved = gitlink.read_lockstate(self.bvc_dir)
        return _LockView(tree, gitlink.tree_hash(tree), found, saved, *self._read_work_lock())

    def _update_lock(self, head: Head, view: _LockView | None = None) -> None:
        # HEAD を書いた直後に bvc.lock を現在位置の版に合わせる(M6-2、仕様書 5.1節、設計書 5節)。
        # - bvc が書いた後で bvc.lock が無くなった(bvc.lock の無いコミットへ checkout した)なら作らず、
        #   lockstate に「bvc.lock が無い」(tree_hash=null)を記録する
        # - 内容がすでに同じなら書き換えない(lockstate だけ更新する)
        # - 非同期状態の bvc.lock を上書きするときは、警告して元の内容を oplog に記録する
        # 書き込みに失敗しても操作は失敗にしない(次に開いたときに _check_lock が書き直す)。
        # view は呼び出し側で読んだ状態(_check_lock から)。無ければここで読む。
        if self._git is None:
            return
        v = view or self._lock_view(head)
        if v is None:
            return
        lock_file = self.config.git.lock_file
        try:
            if v.raw is None and v.found:
                if v.saved is not None:
                    gitlink.write_lockstate(self.bvc_dir, None)
                self.lock_status = "missing"
                return
            lh = v.lh
            if lh != v.th:
                if v.raw is not None and (lh is None or lh != v.saved):
                    logger.warning(
                        "%s が bvc の記録と一致しない状態(非同期状態)でしたが、版 %d の内容で上書きします"
                        "(元の内容は操作ログに記録しました)",
                        lock_file,
                        head.at,
                    )
                    self._history.log_op(
                        {
                            **_op_entry("lock_overwrite", {"lock_file": lock_file}, head),
                            "previous": v.raw.decode("utf-8", "replace"),
                        }
                    )
                manifests = {sha: self._store.get_manifest(sha) for sha in set(v.tree.values())}
                gitlink.write_lock(
                    self.workdir,
                    lock_file,
                    gitlink.lock_bytes(head.at, v.tree, manifests),
                    self.bvc_dir / "tmp",
                )
            if not v.found or v.saved != v.th:
                gitlink.write_lockstate(self.bvc_dir, v.th)
            self.lock_status = "ok"
        except (OSError, CorruptData, UnsafePath) as e:
            logger.warning(
                "%s を更新できませんでした(%s)。次に bvc を実行したときに更新します", lock_file, e
            )

    def _check_lock(self, warn: bool = True) -> None:
        # 開いたときに bvc.lock の状態を調べる(M6-2、設計書 5節)。
        # bvc が最後に書いた内容のまま @ と違う(HEAD の更新後に中断した)なら書き直す。
        # lockstate とも @ とも違えば非同期状態とし、warn なら警告する。
        if self._git is None:
            return
        head = self._history.head()
        v = self._lock_view(head)
        if v is None:
            return
        if v.raw is None:
            if v.found:
                self.lock_status = "missing"
            else:
                self._update_lock(head, v)  # まだ書いていない(init の途中で中断した、手で有効にした)
            return
        lh = v.lh
        if lh is not None and lh in (v.th, v.saved):
            self._update_lock(head, v)
            return
        self.lock_status = "out_of_sync"
        if not warn:
            return
        reason = (
            f"読み込めません({v.err})" if v.err is not None
            else f"現在位置の版 {head.at} と内容が一致しません"
        )
        logger.warning(
            "%s が%s(非同期状態)。git の作業ツリーとバイナリが食い違っている可能性があります。"
            "bvc sync で合わせてください",
            self.config.git.lock_file,
            reason,
        )

    def _find_by_tree(
        self, tree: dict[str, str], hint: int | None, include_discarded: bool = False
    ) -> int | None:
        # 内容が同じ健全な版を探す。hint(bvc_commit)を優先し、無ければ新しい順で最初のもの。
        h = self._history
        candidates = [
            c for c in h.ids(include_discarded) if h.tree_known(c) and h.get(c).tree == tree
        ]
        if hint in candidates:
            candidates.remove(hint)
            candidates.insert(0, hint)
        return next((c for c in candidates if self._is_healthy(c)), None)

    def _check_lock_data(self, lock: LockFile, what: str) -> None:
        # bvc.lock が参照するデータが全部揃っていて、記録(size, sha256)とマニフェストが一致するか。
        # 1つでも欠けていれば CorruptData(何も変えない)。
        missing = []
        for path, e in sorted(lock.files.items()):
            if not self._store.manifest_ok(e.manifest, "exists"):
                missing.append(path)
                continue
            m = self._store.get_manifest(e.manifest)
            if m.size != e.size or m.sha256 != e.sha256:
                raise CorruptData(
                    f"{what} の {path} の記録(サイズ・ハッシュ)が保存データと一致しません(手で編集された可能性があります)",
                    path=path,
                )
        if missing:
            raise CorruptData(
                f"{what} が参照するデータがリポジトリにありません(gc 済み、別のリポジトリの bvc.lock など): "
                + ", ".join(missing),
                missing=missing,
            )

    def sync(
        self,
        allow_missing: bool = False,
        require_lock: bool = False,
        progress: ProgressFn | None = None,
    ) -> SyncResult:
        # bvc.lock の内容に作業ファイルを合わせる(M6-4、仕様書 3.11節)。基準は files の内容。
        # 内容が同じ健全な版へ移動し、無ければデータが揃っている場合だけ import の版を作って移動する。
        # 1つでも欠けている・壊れている・パスが不正・形式が未知なら、何も変えずに中止する。
        self._require_git()
        h = self._history
        head = h.head()
        what = self.config.git.lock_file
        lock = gitlink.read_lock(self._lock_path, what)
        if lock is None:
            msg = f"{what} がありません(bvc.lock の無いコミットです)。作業ファイルはそのままです"
            if require_lock:
                raise BvcError(msg)
            logger.warning("%s", msg)
            return SyncResult(changed=False, before=head, after=head, lock_found=False)
        tree = lock.tree
        hint = lock.bvc_commit
        if hint is not None and not (
            h.exists(hint) and h.tree_known(hint) and h.get(hint).tree == tree
        ):
            logger.warning(
                "%s の bvc_commit(版 %d)と内容が一致しません(別のリポジトリで作られた bvc.lock など)。内容を基準にします",
                what,
                hint,
            )
        if h.tree_known(head.at) and h.get(head.at).tree == tree:
            self._update_lock(head)  # 同じ内容なので書き換えず、lockstate だけ合わせる
            return SyncResult(changed=False, before=head, after=head)
        target = self._find_by_tree(tree, hint)
        args = {"bvc_commit": hint}
        if target is not None:
            res = self._move(
                "sync", "", "", target, h.get(target).branch, allow_missing, progress, args
            )
            return SyncResult(**_fields_of(res))
        self._check_lock_data(lock, what)
        res = self._move(
            "sync", "", "", None, None, allow_missing, progress, args, import_tree=tree
        )
        return SyncResult(**_fields_of(res))

    def install_hooks(self) -> HooksResult:
        # git のフックを設置する(M6-6、仕様書 3.12節)。既存のフックは上書きしない。
        git = self._require_git()
        return gitlink.install_hooks(git.hooks_dir(), self.workdir)

    def git_pin(self) -> PinResult:
        # post-commit: git の HEAD にある bvc.lock と内容が一致する版を pins に記録する(M6-5)。
        if self._git is None:
            return PinResult(changed=False)
        h = self._history
        what = f"HEAD:{self.config.git.lock_file}"
        sha = self._git.head()
        data = self._git.blob_at(sha, self.config.git.lock_file) if sha is not None else None
        if data is None:
            logger.info("git のコミットに %s が無いため、記録しません", self.config.git.lock_file)
            return PinResult(changed=False)
        try:
            lock = gitlink.parse_lock(data, what)
        except (IntegrityError, UnsupportedFormat) as e:
            logger.warning("%s を読み込めないため、記録しません: %s", what, e)
            return PinResult(changed=False)
        cid = self._find_by_tree(lock.tree, lock.bvc_commit, include_discarded=True)
        if cid is None:
            logger.warning("%s と内容が一致する健全な版が無いため、記録しません", what)
            return PinResult(changed=False)
        existing = next((p for p in h.pins() if p.git == sha and p.bvc == cid), None)
        if existing is not None:
            return PinResult(changed=False, pin=existing)
        pin = h.pin(sha, cid, gitlink.tree_hash(lock.tree))
        h.log_op(_op_entry("git_pin", {"git": sha, "bvc": cid}, h.head()))
        return PinResult(changed=True, pin=pin)

    def git_pre_commit(self, progress: ProgressFn | None = None) -> PreCommitResult:
        # pre-commit(M6-6、仕様書 5.3節)。未コミットの変更を自動コミット(snapshot)するか拒否(reject)し、
        # ステージされた bvc.lock が健全な版と一致しなければ拒否する(SafetyAbort → git commit が中止される)。
        if self._git is None:
            return PreCommitResult(changed=False)
        git, cfg = self._git, self.config.git
        result = PreCommitResult(changed=False)
        staged_blobs = git.staged_blobs(cfg.lock_file)
        if cfg.pre_commit == "reject":
            if self.work_state().dirty:
                raise SafetyAbort(
                    "未コミットの変更があるため、git commit を中止します。"
                    "bvc commit で記録してから git commit してください(git.pre_commit=reject)"
                )
        else:
            res = self.commit(message="auto: git pre-commit", kind="auto", progress=progress)
            if res.changed:
                result.changed = True
                result.auto_commit = res.commit
                # bvc.lock を外したコミット(ステージに無い)には加えない
                if staged_blobs and gitlink.read_lock_raw(self._lock_path) is not None:
                    git.add(cfg.lock_file)
        staged = git.staged(cfg.lock_file)
        work, _, raw = self._read_work_lock()
        if staged is None:
            if raw is not None and not staged_blobs:
                logger.warning(
                    "%s が git にステージされていません(git add %s)", cfg.lock_file, cfg.lock_file
                )
            return result
        what = f"ステージされた {cfg.lock_file}"
        try:
            lock = gitlink.parse_lock(staged, what)
            self._check_lock_data(lock, what)
            if self._find_by_tree(lock.tree, lock.bvc_commit, include_discarded=True) is None:
                raise CorruptData(f"{what} と内容が一致する健全な版がありません")
        except (IntegrityError, UnsupportedFormat) as e:
            raise SafetyAbort(
                f"git commit を中止します: {e}。bvc sync で合わせるか、正しい {cfg.lock_file} をステージしてください"
            ) from e
        result.staged_ok = True
        if work is None or work.tree != lock.tree:
            logger.warning(
                "作業ツリーの %s の変更がステージされていません(git add %s)", cfg.lock_file, cfg.lock_file
            )
        return result

    def git_post_checkout(self, progress: ProgressFn | None = None) -> PostCheckoutResult:
        # post-checkout(M6-6)。bvc.lock が現在位置の版と違えば sync する。失敗時の表示は cli が行う。
        if self._git is None:
            return PostCheckoutResult(changed=False)
        lock, err, raw = self._read_work_lock()
        if raw is None:
            logger.info("%s が無いコミットです。作業ファイルはそのままです", self.config.git.lock_file)
            return PostCheckoutResult(changed=False)
        h = self._history
        head = h.head()
        if err is None and h.tree_known(head.at) and h.get(head.at).tree == lock.tree:
            return PostCheckoutResult(changed=False)
        res = self.sync(progress=progress)
        return PostCheckoutResult(changed=res.changed, synced=True, sync=res)

    def _lock_manifests(self, no_git: bool) -> set[str]:
        # gc で保護するマニフェスト: 作業ツリーの bvc.lock と、git の全履歴・ステージの bvc.lock が参照するもの。
        # git の失敗や読めない bvc.lock があれば、何も消さずに中止する(設計書 4.8節)。
        if self._git is None:
            return set()
        what = self.config.git.lock_file
        datas: list[tuple[str, bytes]] = []
        raw = gitlink.read_lock_raw(self._lock_path)
        if raw is not None:
            datas.append((what, raw))
        if not no_git:
            blobs = self._git.history_blobs(what)
            datas += [(f"git の {what}({b[:12]})", d) for b, d in zip(blobs, self._git.cat_blobs(blobs))]
        out: set[str] = set()
        for name, data in datas:
            try:
                out.update(gitlink.parse_lock(data, name).tree.values())
            except (IntegrityError, UnsupportedFormat) as e:
                raise SafetyAbort(
                    f"{name} を読み込めないため、参照されるデータが分かりません。安全のため gc を中止します: {e}"
                ) from e
        return out

    def _tmp_files(self) -> list[Path]:
        # tmp/ の残骸(ロック中なので、書き込み途中のものは無い)。
        tmp = self.bvc_dir / "tmp"
        try:
            with os.scandir(fsutil.os_path(tmp)) as it:
                return sorted(
                    tmp / e.name for e in it if e.is_file(follow_symlinks=False)
                )
        except FileNotFoundError:
            return []
