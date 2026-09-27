# 版のグラフ・HEAD・ブランチ・削除印・pins・操作ログ。設計書 1.1節・3.5節。

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Final, Iterable

from .errors import CorruptData, IntegrityError, RevisionError, UnsafePath
from .fsutil import (
    append_jsonl,
    atomic_write_json,
    check_id,
    check_git_sha,
    check_id_str,
    check_relpath,
    check_sha,
    load_json,
    makedirs,
    now_iso,
    os_path,
    read_jsonl,
)
from .model import Commit, Head, Note, Pin

logger = logging.getLogger(__name__)

KINDS: Final[tuple[str, ...]] = ("init", "commit", "auto", "import")


def check_branch_name(name: Any) -> str:
    # 仕様書 2.3節のブランチ名の制約。数字だけ・'@' で始まる・'+' '-' を含む・空白を含む名前は不可。
    if type(name) is not str or not name:
        raise RevisionError(f"ブランチ名が空か文字列ではありません: {name!r:.80}")
    if (
        name.isdigit()
        or name.startswith("@")
        or "+" in name
        or "-" in name
        or any(c.isspace() for c in name)
    ):
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
    if parent is not None and (
        parent >= commit_id or not ancestors or ancestors[0] != parent
    ):
        raise CorruptData(f"版 {file_id}: 親の記録が不正です")
    if any(b >= a for a, b in zip(ancestors, ancestors[1:])) or any(
        a >= commit_id for a in ancestors
    ):
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

    # パスとハッシュは使う前に検査する(仕様書 2.10節)。不正なら「壊れた版」
    # bvc は NFC で記録するので、正規化で変わるパスも不正とする(同じパスの重複を黙って1つにしないため)
    bad_tree = False
    checked: dict[str, str] = {}
    for path, sha in tree.items():
        try:
            if check_relpath(path) != path:
                raise UnsafePath("")
            checked[path] = check_sha(sha)
        except UnsafePath:
            bad_tree = True
    # 名前変更の記録は表示用だが、パスなので同じ規則で検査する
    checked_renames: list[tuple[str, str, float]] = []
    for r in renames:
        try:
            if (
                not isinstance(r, list)
                or len(r) != 3
                or any(check_relpath(p) != p for p in r[:2])
                or type(r[2]) not in (int, float)
                or not 0 <= r[2] <= 1
            ):
                raise UnsafePath("")
            checked_renames.append((r[0], r[1], r[2]))
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
        renames=tuple(checked_renames),
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
        self._chain: dict[int, tuple[int, ...]] = {}  # 版 → 親から根への番号列
        self._eparent: dict[int, int | None] = {}  # effective parent; 有効な親コミット
        self._children: dict[int, list[int]] = {}

    # --- 読み込み ---

    def load(self) -> None:
        # commits/, discarded.jsonl, branches.json, pins.jsonl を読む(M2-3)。
        commits_dir = self._bvc_dir / "commits"
        with os.scandir(os_path(commits_dir)) as it:
            names = [e.name for e in it if e.name.endswith(".json")]
        for name in names:
            try:
                file_id = check_id_str(name[: -len(".json")])
            except UnsafePath:
                logger.warning(
                    "commits/%s: 版ファイルの名前ではないため無視しました", name
                )
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
                logger.warning(
                    "版 %d: 不正なパスまたはハッシュを含みます(壊れた版として扱います)",
                    file_id,
                )
                self._broken.add(file_id)

        records, warns = read_jsonl(
            self._bvc_dir / "discarded.jsonl", "discarded.jsonl"
        )
        for w in warns:
            logger.warning("%s", w)
        for r in records:
            try:
                self._discarded.add(check_id(r.get("id")))
            except UnsafePath:
                logger.warning(
                    "discarded.jsonl: 不正な番号を読み飛ばしました: %r", r.get("id")
                )

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
                logger.warning(
                    "branches.json: 不正な項目を読み飛ばしました: %r", bid_str
                )

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
                    chain[a] = anc[i + 1 :]
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

    def branch_names(self) -> dict[int, str]:
        return dict(self._branches)

    def branch_by_name(self, name: str) -> int | None:
        # 名前の付いたブランチの番号(無ければ None)。
        return next((b for b, n in self._branches.items() if n == name), None)

    def pinned_ids(self) -> set[int]:
        pinned = set()
        for pin in self._pins:
            try:
                pinned.add(check_id(pin.get("bvc")))
            except UnsafePath:
                pass
        return pinned

    def tree_known(self, commit_id: int) -> bool:
        # 版の tree を全部把握できているか(版ファイルが読めて、不正なパス・ハッシュが無い)。
        # gc が「参照されていない」と判断してよいかの基準にする。
        return (
            self._commits.get(commit_id) is not None and commit_id not in self._broken
        )

    def get_notes(self, commit_id: int) -> list[Note]:
        # コメント(notes/<id>.jsonl)を古い順に返す(M4-1)。壊れた行は読み飛ばす(C-8)。
        what = f"notes/{commit_id}.jsonl"
        records, warns = read_jsonl(self._notes_path(commit_id), what)
        for w in warns:
            logger.warning("%s", w)
        notes = []
        for r in records:
            time, text = r.get("time"), r.get("text")
            if type(time) is str and type(text) is str:
                notes.append(Note(commit_id=commit_id, time=time, text=text))
            else:
                logger.warning("%s: 不正なコメントを読み飛ばしました", what)
        return notes

    def _notes_path(self, commit_id: int) -> Path:
        return self._bvc_dir / "notes" / f"{check_id(commit_id)}.jsonl"

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
            bid = self.branch_by_name(atom)
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
        raise RevisionError(
            f"{expr}: 版 {cur} には子が複数あり、進む先を決められません({kids})"
        )

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
        if os.path.lexists(os_path(commit_file)):
            # 版は不変。既存の版ファイルは決して上書きしない
            raise IntegrityError(
                f"版 {commit_id} のファイルが既にあります。counters.json と版の記録が一致しません"
            )

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
        append_jsonl(
            self._bvc_dir / "oplog.jsonl", {"format": 1, "time": now_iso(), **entry}
        )

    # --- note / discard / branch(M4-1〜M4-3) ---

    def add_note(self, commit_id: int, text: str) -> Note:
        # コメントを追記する(M4-1、設計書 2.6節)。読み込み不可の版にも付けられる(仕様書 2.9節)。
        if commit_id not in self._commits:
            raise RevisionError(f"版 {commit_id} は存在しません")
        note = Note(commit_id=commit_id, time=now_iso(), text=text)
        path = self._notes_path(commit_id)
        makedirs(path.parent)
        append_jsonl(path, {"format": 1, "time": note.time, "text": note.text})
        return note

    def discard(self, commit_id: int) -> None:
        # 削除印を付ける(M4-3、設計書 2.6節)。子は読み込み時のつなぎ直しで親へつながる(4.4節)。
        if commit_id not in self._commits:
            raise RevisionError(f"版 {commit_id} は存在しません")
        if commit_id in self._discarded:
            raise RevisionError(f"版 {commit_id} は削除済みです")
        append_jsonl(
            self._bvc_dir / "discarded.jsonl",
            {"format": 1, "time": now_iso(), "id": commit_id},
        )
        self._discarded.add(commit_id)
        self._rebuild()

    def name_branch(self, branch: int, name: str) -> int | None:
        # ブランチに名前を付ける(M4-2)。同じ名前が別のブランチにあれば付け替え、元の番号を返す。
        # ブランチの古い名前は外れる(1つのブランチに名前は1つ)。
        name = check_branch_name(name)
        check_id(branch)
        previous = self.branch_by_name(name)
        names = {b: n for b, n in self._branches.items() if n != name}
        names[branch] = name
        self._write_branches(names)
        return previous if previous != branch else None

    def unname_branch(self, name: str) -> int:
        # 名前を外し、そのブランチの番号を返す(M4-2)。
        name = check_branch_name(name)
        branch = self.branch_by_name(name)
        if branch is None:
            raise RevisionError(f"ブランチ '{name}' はありません")
        self._write_branches({b: n for b, n in self._branches.items() if b != branch})
        return branch

    def _write_branches(self, names: dict[int, str]) -> None:
        # 書き込みに成功してから、メモリ上の名前を置き換える。
        atomic_write_json(
            self._bvc_dir / "branches.json",
            {"format": 1, "names": {str(b): n for b, n in sorted(names.items())}},
            self._tmp,
        )
        self._branches = names

    # --- gc 用(M4-4、設計書 4.8節) ---

    def commit_paths(self, commit_id: int) -> list[Path]:
        # 版を消すときに削除するファイル(版ファイル、コメント)。版ファイルを先に消す。
        cid = check_id(commit_id)
        return [self._bvc_dir / "commits" / f"{cid}.json", self._notes_path(cid)]

    def note_ids(self) -> list[int]:
        # notes/ にあるコメントの版番号(名前が不正なファイルは無視する)。
        try:
            with os.scandir(os_path(self._bvc_dir / "notes")) as it:
                names = [e.name for e in it if e.name.endswith(".jsonl")]
        except FileNotFoundError:
            return []
        ids = []
        for name in names:
            try:
                ids.append(check_id_str(name[: -len(".jsonl")]))
            except UnsafePath:
                pass
        return sorted(ids)

    # --- 管理ファイルの自動復旧(M4-10、設計書 4.12節) ---
    # 検査の関数は、異常の説明(無ければ None)を返す。無い・解析できない・形式が不正な場合だけを異常とし、
    # 知らない format(UnsupportedFormat)と読み込みの OSError はそのまま送出する(D-15、I-20)。

    def check_branches(self) -> str | None:
        # branches.json の異常(load の前に呼び出す)。個々の不正な項目は load で読み飛ばす。
        try:
            data = load_json(self._bvc_dir / "branches.json", "branches.json")
        except FileNotFoundError:
            return "ファイルがありません"
        except CorruptData as e:
            return str(e)
        if not isinstance(data.get("names"), dict):
            return "branches.json: names が不正です"
        return None

    def reset_branches(self) -> None:
        # branches.json を空で作り直す(名前は失われる)。
        self._write_branches({})

    def check_counters(self) -> str | None:
        try:
            data = load_json(self._bvc_dir / "counters.json", "counters.json")
            check_id(data["next_commit"])
            check_id(data["next_branch"])
        except FileNotFoundError:
            return "ファイルがありません"
        except CorruptData as e:
            return str(e)
        except (KeyError, UnsafePath) as e:
            return f"counters.json: 内容が不正です({e})"
        return None

    def rebuild_counters(self) -> dict[str, int]:
        # 版・削除印・操作ログに現れる最大の番号から counters.json を作り直す(load の後に呼び出す)。
        # gc で消えた版の番号も、削除印と操作ログに残っているので再利用しない。
        commit_ids = set(self._commits) | set(self._discarded)
        branch_ids = {c.branch for c in self._commits.values() if c is not None} | set(
            self._branches
        )
        for r in self._oplog():
            for v in r.get("created") or ():
                if type(v) is int and 0 <= v:
                    commit_ids.add(v)
            for key in ("before", "after"):
                h = r.get(key)
                if isinstance(h, dict):
                    for field, ids in (("at", commit_ids), ("branch", branch_ids)):
                        v = h.get(field)
                        if type(v) is int and 0 <= v:
                            ids.add(v)
        counters = {
            "next_commit": max(commit_ids, default=-1) + 1,
            "next_branch": max(branch_ids, default=-1) + 1,
        }
        atomic_write_json(
            self._bvc_dir / "counters.json", {"format": 1, **counters}, self._tmp
        )
        return counters

    def check_head(self) -> str | None:
        # HEAD.json の異常。存在しない版・削除済みの版を指している場合も異常とする(load の後に呼び出す)。
        try:
            head = self.head()
        except FileNotFoundError:
            return "ファイルがありません"
        except CorruptData as e:
            return str(e)
        if head.at not in self._commits:
            return f"HEAD.json: 存在しない版 {head.at} を指しています"
        if head.at in self._discarded:
            return f"HEAD.json: 削除済みの版 {head.at} を指しています"
        return None

    def head_from_oplog(self) -> Head | None:
        # 操作ログの最後の after(現在位置の記録)。使えない値なら None。
        for r in reversed(self._oplog()):
            after = r.get("after")
            if not isinstance(after, dict):
                continue
            try:
                head = Head(
                    at=check_id(after.get("at")), branch=check_id(after.get("branch"))
                )
            except UnsafePath:
                return None
            if head.at in self._commits and head.at not in self._discarded:
                return head
            return None
        return None

    def _oplog(self) -> list[dict]:
        records, warns = read_jsonl(self._bvc_dir / "oplog.jsonl", "oplog.jsonl")
        for w in warns:
            logger.warning("%s", w)
        return records

    def pins(self) -> list[Pin]:
        # pins.jsonl の記録(古い順)。不正な行は読み飛ばす。
        out = []
        for r in self._pins:
            try:
                out.append(
                    Pin(
                        git=check_git_sha(r.get("git")),
                        bvc=check_id(r.get("bvc")),
                        tree_hash=check_sha(r.get("tree_hash")),
                    )
                )
            except UnsafePath:
                logger.warning("pins.jsonl: 不正な記録を読み飛ばしました")
        return out

    def pin(self, git_sha: str, bvc_id: int, tree_hash: str) -> Pin:
        # git のコミットと版の対応を pins.jsonl に追記する(M6-5、設計書 2.6節)。
        if bvc_id not in self._commits:
            raise RevisionError(f"版 {bvc_id} は存在しません")
        pin = Pin(
            git=check_git_sha(git_sha), bvc=bvc_id, tree_hash=check_sha(tree_hash)
        )
        record = {
            "format": 1,
            "time": now_iso(),
            "git": pin.git,
            "bvc": pin.bvc,
            "tree_hash": pin.tree_hash,
        }
        append_jsonl(self._bvc_dir / "pins.jsonl", record)
        self._pins.append(record)
        return pin
