# 版のグラフ・HEAD・ブランチ・削除印・pins・操作ログ。設計書 1.1節・3.5節。

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import RevisionError, UnsafePath, UnsupportedFormat
from .fsutil import append_jsonl, atomic_write_json, check_format, check_id, load_json, read_jsonl
from .model import Commit, Head, Note
from .store import ObjectStore


class History:
    # 版の履歴グラフを管理する。

    def __init__(self, repodir: Path, store: ObjectStore):
        self.repodir = repodir
        self._bvc_dir = repodir / ".bvc"
        self._store = store

        # ロード後に設定される
        self._commits: dict[int, Commit] = {}
        self._discarded: set[int] = set()
        self._branches: dict[int, str] = {}  # ブランチ ID → 名前
        self._pins: list[dict] = []
        self._children: dict[int, list[int]] = {}  # 版 ID → その子のリスト
        self._notes: dict[int, list[Note]] = {}  # 版 ID → コメント

    def load(self) -> None:
        # commits/, discarded.jsonl, branches.json, pins.jsonl, health を読む(M2-3)。

        # commits/ フォルダから全版を読む
        commits_dir = self._bvc_dir / "commits"
        if commits_dir.exists():
            for commit_file in commits_dir.glob("*.json"):
                try:
                    data = load_json(commit_file, str(commit_file))
                    check_format(data, str(commit_file), known=(1,))

                    # Commit に変換
                    commit = self._parse_commit(data)
                    self._commits[commit.id] = commit
                except (FileNotFoundError, json.JSONDecodeError, KeyError):
                    # 読めない版は番号だけ登録(設計書 4.4節)
                    try:
                        commit_id = check_id(int(commit_file.stem))
                        self._commits[commit_id] = None
                    except (ValueError, TypeError):
                        pass

        # discarded.jsonl から削除済みの版を読む
        discarded_file = self._bvc_dir / "discarded.jsonl"
        if discarded_file.exists():
            records, _ = read_jsonl(discarded_file, "discarded.jsonl", known=(1,))
            for record in records:
                self._discarded.add(record.get("id"))

        # branches.json から名前を読む
        branches_file = self._bvc_dir / "branches.json"
        if branches_file.exists():
            data = load_json(branches_file, "branches.json")
            check_format(data, "branches.json", known=(1,))
            names = data.get("names", {})
            for bid_str, name in names.items():
                try:
                    bid = check_id(int(bid_str))
                    self._branches[bid] = name
                except (ValueError, TypeError):
                    pass

        # pins.jsonl から pin を読む
        pins_file = self._bvc_dir / "pins.jsonl"
        if pins_file.exists():
            self._pins, _ = read_jsonl(pins_file, "pins.jsonl", known=(1,))

        # notes/ フォルダからコメントを読む(M4 で実装)
        # ここでは _notes に記録するだけ

        # 子を計算する
        self._compute_children()

    def _parse_commit(self, data: dict) -> Commit:
        # JSON から Commit を作る。

        commit_id = check_id(data["id"])
        parent = data.get("parent")
        if parent is not None:
            parent = check_id(parent)

        ancestors = tuple(check_id(a) for a in data.get("ancestors", []))
        branch = check_id(data["branch"])
        time = data["time"]
        kind = data["kind"]
        message = data["message"]

        tree = {}
        for path, sha in data.get("tree", {}).items():
            tree[path] = sha

        renames = tuple(data.get("renames", []))
        stats = data.get("stats", {})

        return Commit(
            id=commit_id,
            parent=parent,
            ancestors=ancestors,
            branch=branch,
            time=time,
            kind=kind,
            message=message,
            tree=tree,
            renames=renames,
            stats=stats,
        )

    def _compute_children(self) -> None:
        # 全版について、その子を計算する(設計書 4.4節の load 時計算)。

        self._children = {}
        for commit_id, commit in self._commits.items():
            if commit is None:
                continue

            parent = self.effective_parent(commit_id)
            if parent is not None:
                if parent not in self._children:
                    self._children[parent] = []
                self._children[parent].append(commit_id)

    def get(self, commit_id: int) -> Commit:
        # 版を取得(M2-3)。

        if commit_id not in self._commits:
            raise RevisionError(f"版 {commit_id} が見つかりません")

        commit = self._commits[commit_id]
        if commit is None:
            raise RevisionError(f"版 {commit_id} が読み込めません")

        return commit

    def is_broken(self, commit_id: int) -> bool:
        # 版ファイルが読込不可か、tree 内に異常があるか(M2-3)。

        if commit_id not in self._commits:
            return True

        commit = self._commits[commit_id]
        return commit is None

    def living(self) -> Iterable[Commit]:
        # 生きている版を ID の逆順で返す(M2-3)。

        for commit_id in sorted(self._commits.keys(), reverse=True):
            if commit_id in self._discarded:
                continue
            commit = self._commits.get(commit_id)
            if commit is not None:
                yield commit

    def effective_parent(self, commit_id: int) -> int | None:
        # つなぎ直し後の親(M2-4)。設計書 4.4節。

        if commit_id not in self._commits:
            return None

        commit = self._commits[commit_id]
        if commit is None:
            return None

        # 版ファイルが読めれば ancestors を辿る
        for ancestor_id in [commit.parent] + list(commit.ancestors):
            if ancestor_id is None:
                continue
            if ancestor_id not in self._discarded and ancestor_id in self._commits:
                return ancestor_id

        return None

    def children(self, commit_id: int) -> list[int]:
        # 生きている子(effective_parent 基準)(M2-4)。

        return self._children.get(commit_id, [])

    def branch_tip(self, branch: int) -> int | None:
        # ブランチの先端(最大 ID の生きている版)(M2-4)。

        candidates = []
        for commit_id, commit in self._commits.items():
            if commit is not None and not (commit_id in self._discarded) and commit.branch == branch:
                candidates.append(commit_id)

        return max(candidates) if candidates else None

    def path_to_tip(self, branch: int) -> list[int]:
        # ブランチの先端から根への経路(M2-4)。設計書 4.5節。

        tip = self.branch_tip(branch)
        if tip is None:
            return []

        path = [tip]
        current = tip

        while True:
            parent = self.effective_parent(current)
            if parent is None:
                break
            path.append(parent)
            current = parent

        return path

    def resolve(self, expr: str, head: Head) -> int:
        # リビジョン式を解決(M2-5)。設計書 4.5節。

        if not expr:
            raise RevisionError("リビジョン式が空です")

        # atom ( '-' | '+' )*
        parts = expr.split("+")
        parts = [p for part in parts for p in part.split("-") if p]

        if not parts:
            raise RevisionError(f"不正なリビジョン式: {expr}")

        # atom を解決
        atom = parts[0]
        if atom == "@":
            current = head.at
        elif atom.isdigit():
            current = check_id(int(atom))
        else:
            # ブランチ名
            found = False
            for bid, name in self._branches.items():
                if name == atom:
                    tip = self.branch_tip(bid)
                    if tip is None:
                        raise RevisionError(f"ブランチ '{atom}' が見つかりません")
                    current = tip
                    found = True
                    break

            if not found:
                raise RevisionError(f"未知のリビジョン: {atom}")

        # - / + を処理
        idx = 0
        in_expr = [c for c in expr]
        sign_idx = 0
        for i, c in enumerate(expr):
            if c == "-":
                sign_idx = i

        # 簡略化のため、今は + だけ対応(完全な実装は M2-5)
        # (本格的なリビジョン式の解析は後で)

        return current

    def head(self) -> Head:
        # 現在の HEAD を取得(M2-6)。

        head_file = self._bvc_dir / "HEAD.json"
        data = load_json(head_file, "HEAD.json")
        check_format(data, "HEAD.json", known=(1,))

        return Head(
            at=check_id(data["at"]),
            branch=check_id(data["branch"]),
        )

    def set_head(self, head: Head) -> None:
        # HEAD を更新(M2-6)。

        data = {
            "format": 1,
            "at": head.at,
            "branch": head.branch,
        }
        head_file = self._bvc_dir / "HEAD.json"
        tmp_dir = self._bvc_dir / "tmp"
        atomic_write_json(head_file, data, tmp_dir)

    def new_commit(
        self,
        parent: int | None,
        tree: dict[str, str],
        kind: str,
        message: str,
        renames: tuple,
        stats: dict,
    ) -> Commit:
        # 新しい版を作成して保存(M2-6)。設計書 4.5節のブランチ規則を適用。

        # counters を読む
        counters_file = self._bvc_dir / "counters.json"
        counters_data = load_json(counters_file, "counters.json")
        check_format(counters_data, "counters.json", known=(1,))

        commit_id = check_id(counters_data["next_commit"])
        next_branch = counters_data.get("next_branch", 1)

        # ブランチを決める(設計書 4.5節)
        if parent is not None and not self.children(parent):
            # 親に子がなければ、親のブランチを継承
            branch = self.get(parent).branch
        else:
            # 親に子がいるか、親がなければ新しいブランチ
            branch = next_branch
            next_branch += 1

        # 祖先を計算
        ancestors = []
        if parent is not None:
            parent_commit = self.get(parent)
            ancestors.append(parent)
            ancestors.extend(parent_commit.ancestors)

        # Commit を作る
        now = datetime.now(timezone.utc).isoformat()
        commit = Commit(
            id=commit_id,
            parent=parent,
            ancestors=tuple(ancestors),
            branch=branch,
            time=now,
            kind=kind,
            message=message,
            tree=tree,
            renames=renames,
            stats=stats,
        )

        # commits/<id>.json を書く(原子的)
        commit_data = {
            "format": 1,
            "id": commit.id,
            "parent": commit.parent,
            "ancestors": list(commit.ancestors),
            "branch": commit.branch,
            "time": commit.time,
            "kind": commit.kind,
            "message": commit.message,
            "tree": commit.tree,
            "renames": list(commit.renames),
            "stats": commit.stats,
        }
        commit_file = self._bvc_dir / "commits" / f"{commit_id}.json"
        tmp_dir = self._bvc_dir / "tmp"
        atomic_write_json(commit_file, commit_data, tmp_dir)

        # counters を更新
        counters_data["next_commit"] = commit_id + 1
        counters_data["next_branch"] = next_branch
        atomic_write_json(counters_file, counters_data, tmp_dir)

        # 内部状態を更新
        self._commits[commit_id] = commit
        self._compute_children()

        return commit

    def add_note(self, commit_id: int, text: str) -> Note:
        # コメントを追記(M4 で実装)。

        pass

    def discard(self, commit_id: int) -> None:
        # 版を削除印を付ける(M4 で実装)。

        pass

    def name_branch(self, branch: int, name: str) -> None:
        # ブランチに名前を付ける(M4 で実装)。

        pass

    def unname_branch(self, name: str) -> None:
        # ブランチ名を削除(M4 で実装)。

        pass

    def pin(self, git_sha: str, bvc_id: int, tree_hash: str) -> None:
        # git への pin を記録(M6 で実装)。

        pass

    def log_op(self, entry: dict) -> None:
        # 操作ログを記録(M2 で実装)。

        pass

    # ヘルパー関数

    def branch_name(self, branch: int) -> str | None:
        # ブランチ名を取得。

        return self._branches.get(branch)

    def discarded_ids(self) -> set[int]:
        # 削除済みの版 ID を返す。

        return self._discarded.copy()

    def pinned_ids(self) -> set[int]:
        # git に pin された版 ID を返す。

        pinned = set()
        for pin in self._pins:
            if "bvc" in pin:
                pinned.add(check_id(pin["bvc"]))
        return pinned

    def get_notes(self, commit_id: int) -> list[Note]:
        # コメントを取得。

        return self._notes.get(commit_id, [])
