# 版のグラフ・HEAD・ブランチ・削除印・pins・操作ログ。設計書 1.1節・3.5節。

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import CorruptData, IntegrityError, RevisionError, UnsafePath
from .fsutil import (
    append_jsonl,
    atomic_write_json,
    check_id,
    check_id_str,
    check_relpath,
    check_sha,
    load_json,
    now_iso,
    read_jsonl,
)
from .model import Commit, Head, Note

logger = logging.getLogger(__name__)

KINDS = ("init", "commit", "auto", "import")


def check_branch_name(name: Any) -> str:
    # 仕様書 2.3節のブランチ名の制約。数字だけ・'@' で始まる・'+' '-' を含む・空白を含む名前は不可。
    if type(name) is not str or not name:
        raise RevisionError(f"ブランチ名が空か文字列ではありません: {name!r:.80}")
    if name.isdigit() or name.startswith("@") or "+" in name or "-" in name or any(c.isspace() for c in name):
        raise RevisionError(f"ブランチ名に使えない形式です: {name!r}")
    return name


def _parse_commit(data: dict, file_id: int) -> tuple[Commit, bool]:
    # JSON から Commit を作る。(Commit, tree に不正な値があるか) を返す。
    # 番号・親・祖先・ブランチが読めなければ CorruptData(版として使えない)。
    try:
        commit_id = check_id(data["id"])
        parent = data["parent"]
        if parent is not None:
            parent = check_id(parent)
        ancestors = tuple(check_id(a) for a in data["ancestors"])
        branch = check_id(data["branch"])
        time = data["time"]
        kind = data["kind"]
        message = data["message"]
        tree = data["tree"]
        renames = data.get("renames", [])
        stats = data.get("stats", {})
    except (KeyError, TypeError, UnsafePath) as e:
        raise CorruptData(f"版 {file_id}: 内容が不正です({e})") from e
    if commit_id != file_id:
        raise CorruptData(f"版 {file_id}: ファイル名と番号が一致しません({commit_id})")
    # 祖先は自分より小さい番号だけ(壊れたデータでグラフが循環しないように)
    if parent is not None and (parent >= commit_id or not ancestors or ancestors[0] != parent):
        raise CorruptData(f"版 {file_id}: 親の記録が不正です")
    if any(b >= a for a, b in zip(ancestors, ancestors[1:])) or any(a >= commit_id for a in ancestors):
        raise CorruptData(f"版 {file_id}: 祖先の記録が不正です")
    if (
        type(time) is not str
        or kind not in KINDS
        or type(message) is not str
        or not isinstance(tree, dict)
        or not isinstance(renames, list)
        or not isinstance(stats, dict)
    ):
        raise CorruptData(f"版 {file_id}: 内容が不正です")

    # パスとハッシュは使う前に検査する(仕様書 2.9節)。不正なら「壊れた版」
    bad_tree = False
    checked: dict[str, str] = {}
    for path, sha in tree.items():
        try:
            checked[check_relpath(path)] = check_sha(sha)
        except UnsafePath:
            bad_tree = True
    commit = Commit(
        id=commit_id,
        parent=parent,
        ancestors=ancestors,
        branch=branch,
        time=time,
        kind=kind,
        message=message,
        tree=checked,
        renames=tuple(tuple(r) for r in renames if isinstance(r, list)),
        stats=stats,
    )
    return commit, bad_tree


def _commit_to_json(c: Commit) -> dict:
    return {
        "format": 1,
        "id": c.id,
        "parent": c.parent,
        "ancestors": list(c.ancestors),
        "branch": c.branch,
        "time": c.time,
        "kind": c.kind,
        "message": c.message,
        "tree": c.tree,
        "renames": [list(r) for r in c.renames],
        "stats": c.stats,
    }


class History:
    # 版の履歴グラフを管理する。bvc_dir は .bvc フォルダ。

    def __init__(self, bvc_dir: Path):
        self._bvc_dir = Path(bvc_dir)
        self._tmp = self._bvc_dir / "tmp"
        # 読めない版は None で番号だけ登録する(設計書 4.4節)
        self._commits: dict[int, Commit | None] = {}
        self._broken: set[int] = set()
        self._discarded: set[int] = set()
        self._branches: dict[int, str] = {}
        self._pins: list[dict] = []
        self._chain: dict[int, tuple[int, ...]] = {}   # 版 → 親から根への番号列
        self._eparent: dict[int, int | None] = {}
        self._children: dict[int, list[int]] = {}

    # --- 読み込み ---

    def load(self) -> None:
        # commits/, discarded.jsonl, branches.json, pins.jsonl を読む(M2-3)。
        commits_dir = self._bvc_dir / "commits"
        with os.scandir(commits_dir) as it:
            names = [e.name for e in it if e.name.endswith(".json")]
        for name in names:
            try:
                file_id = check_id_str(name[: -len(".json")])
            except UnsafePath:
                logger.warning("commits/%s: 版ファイルの名前ではないため無視しました", name)
                continue
            try:
                data = load_json(commits_dir / name, f"版 {file_id}")
                commit, bad_tree = _parse_commit(data, file_id)
            except CorruptData as e:
                # UnsupportedFormat と OSError(使用中など)はそのまま送出する(D-15)
                logger.warning("%s(読み込み不可として扱います)", e)
                self._commits[file_id] = None
                continue
            self._commits[file_id] = commit
            if bad_tree:
                logger.warning("版 %d: 不正なパスまたはハッシュを含みます(壊れた版として扱います)", file_id)
                self._broken.add(file_id)

        records, warns = read_jsonl(self._bvc_dir / "discarded.jsonl", "discarded.jsonl")
        for w in warns:
            logger.warning("%s", w)
        for r in records:
            try:
                self._discarded.add(check_id(r.get("id")))
            except UnsafePath:
                logger.warning("discarded.jsonl: 不正な番号を読み飛ばしました: %r", r.get("id"))

        try:
            data = load_json(self._bvc_dir / "branches.json", "branches.json")
            names_obj = data.get("names", {})
            if not isinstance(names_obj, dict):
                raise CorruptData("branches.json: names が不正です")
        except FileNotFoundError:
            names_obj = {}
        except CorruptData as e:
            logger.warning("%s(ブランチ名を読み込めません)", e)
            names_obj = {}
        for bid_str, name in names_obj.items():
            try:
                self._branches[check_id_str(bid_str)] = check_branch_name(name)
            except (UnsafePath, RevisionError):
                logger.warning("branches.json: 不正な項目を読み飛ばしました: %r", bid_str)

        self._pins, warns = read_jsonl(self._bvc_dir / "pins.jsonl", "pins.jsonl")
        for w in warns:
            logger.warning("%s", w)

        self._rebuild()

    def _rebuild(self) -> None:
        # 祖先の列・つなぎ直し後の親・子の一覧を全版について計算する(設計書 4.4節)。
        chain: dict[int, tuple[int, ...]] = {}
        for cid, c in self._commits.items():
            if c is not None:
                chain[cid] = c.ancestors
        # 読めない版の祖先は、子の ancestors から補う
        for cid in sorted(chain, reverse=True):
            anc = chain[cid]
            for i, a in enumerate(anc):
                if a not in chain:
                    chain[a] = anc[i + 1:]
        self._chain = chain

        self._eparent = {}
        self._children = {}
        for cid in self._commits:
            p = None
            for a in chain.get(cid, ()):
                if a not in self._discarded and a in self._commits:
                    p = a
                    break
            self._eparent[cid] = p
            if p is not None and cid not in self._discarded:
                self._children.setdefault(p, []).append(cid)
        for kids in self._children.values():
            kids.sort()

    # --- 参照 ---

    def exists(self, commit_id: int) -> bool:
        return commit_id in self._commits

    def ids(self, include_discarded: bool = False) -> list[int]:
        # 版の番号を新しい順に返す(読めない版を含む)。
        return sorted(
            (i for i in self._commits if include_discarded or i not in self._discarded),
            reverse=True,
        )

    def get(self, commit_id: int) -> Commit:
        # 版を取得(M2-3)。無い・読めない版は RevisionError。
        if commit_id not in self._commits:
            raise RevisionError(f"版 {commit_id} は存在しません")
        commit = self._commits[commit_id]
        if commit is None:
            raise RevisionError(f"版 {commit_id} は読み込めません")
        return commit

    def is_readable(self, commit_id: int) -> bool:
        return self._commits.get(commit_id) is not None

    def is_broken(self, commit_id: int) -> bool:
        # 版ファイルが読めない、または tree に不正な値がある(M2-3)。
        # マニフェスト・チャンクの健全性は M3 以降で health と合わせて判定する。
        return self._commits.get(commit_id) is None or commit_id in self._broken

    def living(self) -> Iterable[Commit]:
        # 生きている(読める)版を新しい順に返す(M2-3)。
        for cid in self.ids():
            c = self._commits[cid]
            if c is not None:
                yield c

    def is_discarded(self, commit_id: int) -> bool:
        return commit_id in self._discarded

    def effective_parent(self, commit_id: int) -> int | None:
        # つなぎ直し後の親(M2-4)。設計書 4.4節。
        return self._eparent.get(commit_id)

    def children(self, commit_id: int) -> list[int]:
        # 生きている子(effective_parent 基準)(M2-4)。
        return list(self._children.get(commit_id, []))

    def branch_tip(self, branch: int) -> int | None:
        # ブランチの先端(生きている版のうち最大の番号)(M2-4)。
        tips = [
            cid
            for cid, c in self._commits.items()
            if c is not None and c.branch == branch and cid not in self._discarded
        ]
        return max(tips) if tips else None

    def path_to_tip(self, branch: int) -> list[int]:
        # ブランチの先端から根への経路(M2-4)。
        tip = self.branch_tip(branch)
        path: list[int] = []
        cur = tip
        while cur is not None:
            path.append(cur)
            cur = self._eparent.get(cur)
        return path

    def branch_name(self, branch: int) -> str | None:
        return self._branches.get(branch)

    def pinned_ids(self) -> set[int]:
        pinned = set()
        for pin in self._pins:
            try:
                pinned.add(check_id(pin.get("bvc")))
            except UnsafePath:
                pass
        return pinned

    def get_notes(self, commit_id: int) -> list[Note]:
        # コメントの読み込みは M4 で実装する。
        return []

    # --- リビジョン式(M2-5、仕様書 2.3節、設計書 4.5節) ---

    def resolve(self, expr: str, head: Head) -> int:
        # atom ('-' | '+')*。atom は '@'、版番号、ブランチ名。
        if type(expr) is not str or not expr:
            raise RevisionError("リビジョン式が空です")
        i = len(expr)
        while i > 0 and expr[i - 1] in "+-":
            i -= 1
        atom, ops = expr[:i], expr[i:]
        if not atom:
            raise RevisionError(f"リビジョン式が不正です: {expr!r}")

        if atom == "@":
            cur = head.at
            if cur not in self._commits:
                raise RevisionError(f"現在位置の版 {cur} が存在しません")
        elif atom.isdigit():
            try:
                cur = check_id_str(atom)
            except UnsafePath:
                raise RevisionError(f"版番号が不正です: {atom!r}") from None
            if cur not in self._commits:
                raise RevisionError(f"版 {cur} は存在しません")
            if cur in self._discarded:
                raise RevisionError(f"版 {cur} は削除済みです")
        else:
            try:
                check_branch_name(atom)
            except RevisionError:
                raise RevisionError(f"リビジョン式が不正です: {expr!r}") from None
            bid = next((b for b, n in self._branches.items() if n == atom), None)
            if bid is None:
                raise RevisionError(f"ブランチ '{atom}' はありません")
            cur = self.branch_tip(bid)
            if cur is None:
                raise RevisionError(f"ブランチ '{atom}' に生きている版がありません")

        for op in ops:
            if op == "-":
                p = self._eparent.get(cur)
                if p is None:
                    raise RevisionError(f"{expr}: 版 {cur} は根なので親がありません")
                cur = p
            else:
                cur = self._forward(cur, head.branch, expr)
        return cur

    def _forward(self, cur: int, branch: int, expr: str) -> int:
        # '+': HEAD.branch の経路上ならその先端方向へ、そうでなければ子が1つのときだけ進む。
        path = self.path_to_tip(branch)
        if cur in path:
            idx = path.index(cur)
            if idx > 0:
                return path[idx - 1]
        kids = self.children(cur)
        if len(kids) == 1:
            return kids[0]
        if not kids:
            raise RevisionError(f"{expr}: 版 {cur} は先端なので子がありません")
        raise RevisionError(f"{expr}: 版 {cur} には子が複数あり、進む先を決められません({kids})")

    # --- HEAD ---

    def head(self) -> Head:
        data = load_json(self._bvc_dir / "HEAD.json", "HEAD.json")
        try:
            return Head(at=check_id(data["at"]), branch=check_id(data["branch"]))
        except (KeyError, UnsafePath) as e:
            raise CorruptData(f"HEAD.json: 内容が不正です({e})") from e

    def set_head(self, head: Head) -> None:
        atomic_write_json(
            self._bvc_dir / "HEAD.json",
            {"format": 1, "at": head.at, "branch": head.branch},
            self._tmp,
        )

    # --- 書き込み ---

    def new_commit(
        self,
        parent: int | None,
        tree: dict[str, str],
        kind: str,
        message: str,
        renames: tuple = (),
        stats: dict | None = None,
    ) -> Commit:
        # 新しい版を作成して保存する(M2-6)。書き込み順は counters → 版(設計書 4.7節)。
        # counters を先に進めるので、途中で中断しても同じ番号の版を書き直すことはない。
        if kind not in KINDS:
            raise ValueError(f"不明な種別: {kind!r}")
        counters_file = self._bvc_dir / "counters.json"
        counters = load_json(counters_file, "counters.json")
        try:
            next_commit = check_id(counters["next_commit"])
            next_branch = check_id(counters["next_branch"])
        except (KeyError, UnsafePath) as e:
            raise CorruptData(f"counters.json: 内容が不正です({e})") from e
        # 既存の番号とは必ず重ならないようにする(counters が古い場合の保険)
        known_branches = [c.branch for c in self._commits.values() if c is not None]
        commit_id = max([next_commit] + [i + 1 for i in self._commits])
        next_branch = max([next_branch] + [b + 1 for b in known_branches])

        # ブランチ規則(設計書 4.5節): 親に生きている子が無ければ親のブランチを延長する
        if parent is None:
            ancestors: tuple[int, ...] = ()
            branch = next_branch
            next_branch += 1
        else:
            if parent not in self._commits:
                raise RevisionError(f"親の版 {parent} は存在しません")
            ancestors = (parent,) + self._chain.get(parent, ())
            parent_commit = self._commits[parent]
            if parent_commit is not None and not self.children(parent):
                branch = parent_commit.branch
            else:
                branch = next_branch
                next_branch += 1

        commit = Commit(
            id=commit_id,
            parent=parent,
            ancestors=ancestors,
            branch=branch,
            time=now_iso(),
            kind=kind,
            message=message,
            tree=dict(tree),
            renames=tuple(renames),
            stats=dict(stats or {}),
        )
        commit_file = self._bvc_dir / "commits" / f"{commit_id}.json"
        if commit_file.exists():
            # 版は不変。既存の版ファイルは決して上書きしない
            raise IntegrityError(f"版 {commit_id} のファイルが既にあります。counters.json と版の記録が一致しません")

        atomic_write_json(
            counters_file,
            {"format": 1, "next_commit": commit_id + 1, "next_branch": next_branch},
            self._tmp,
        )
        atomic_write_json(commit_file, _commit_to_json(commit), self._tmp)

        self._commits[commit_id] = commit
        self._rebuild()
        return commit

    def log_op(self, entry: dict) -> None:
        # 操作ログ(oplog.jsonl)に1行追記する(M2-6、設計書 2.6節)。
        append_jsonl(self._bvc_dir / "oplog.jsonl", {"format": 1, "time": now_iso(), **entry})

    # --- M4 以降 ---

    def add_note(self, commit_id: int, text: str) -> Note:
        raise NotImplementedError("note は M4 で実装する")

    def discard(self, commit_id: int) -> None:
        raise NotImplementedError("discard は M4 で実装する")

    def name_branch(self, branch: int, name: str) -> None:
        raise NotImplementedError("branch は M4 で実装する")

    def unname_branch(self, name: str) -> None:
        raise NotImplementedError("branch は M4 で実装する")

    def pin(self, git_sha: str, bvc_id: int, tree_hash: str) -> None:
        raise NotImplementedError("pin は M6 で実装する")
