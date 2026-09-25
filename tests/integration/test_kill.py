# 強制終了テスト(M3-9、実装計画書 6.3節)。観点: R-1, R-2, R-3。
# 子プロセス(kill_driver.py)を指定の段階で kill し、次に開いたときに収束することを確かめる。

from __future__ import annotations

from pathlib import Path

import pytest

from bvc.model import Head
from bvc.repo import Repo
from tests import helpers

pytestmark = pytest.mark.kill

# 版 0: a=a0, b=b0, d/c=c0
# 版 1: a=a1, b=b1, e=e1(d/c は削除)
# 作業フォルダ: 版 1 から a を編集した状態(goto 0 で自動コミット 2 ができる)
V0 = {"a.bin": b"a0", "b.bin": b"b0", "d/c.bin": b"c0"}
EDITED = {"a.bin": b"a-edit", "b.bin": b"b1", "e.bin": b"e1"}


def write(root: Path, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def files(root: Path) -> dict[str, bytes]:
    return {p: (root / p).read_bytes() for p in helpers.tree_hashes(root)}


def build(root: Path) -> None:
    for rel, data in V0.items():
        write(root, rel, data)
    with Repo.init(root, track=["**/*.bin"]) as repo:
        write(root, "a.bin", b"a1")
        write(root, "b.bin", b"b1")
        (root / "d" / "c.bin").unlink()
        write(root, "e.bin", b"e1")
        repo.commit("v1", allow_missing=True)
    write(root, "a.bin", b"a-edit")


def check_converged(root: Path) -> Head:
    # 開き直して(recover)、中途半端な状態が残っていないことを確かめる。HEAD を返す。
    with Repo.open(root) as repo:
        head = repo._history.head()
        state = repo.work_state()
        current = files(root)
        if head.at == 0:
            assert current == V0
            assert not state.dirty
        else:
            assert current == EDITED
            # 自動コミットの前に kill したなら、編集は未コミットのまま残る
            assert state.dirty == (head.at == 1)
        assert not (repo.bvc_dir / "journal.json").exists()
        assert list((repo.bvc_dir / "txn").iterdir()) == []
        assert repo._store.verify_all().ok
        # 続けて操作できる
        repo.goto("0")
        assert files(root) == V0
    return head


# (段階名, 回数, 収束後の HEAD)
# HEAD 1: 自動コミットの HEAD 更新の前。2: 自動コミットの後、swapped の前(元に戻す)。0: swapped の後(完了させる)
GOTO_STAGES = (
    [("chunk_write", 1, 1), (r"atomic_write:[0-9a-f]{64}\.json", 1, 1), ("atomic_write:counters.json", 1, 1),
     (r"atomic_write:\d+\.json", 1, 1), ("atomic_write:HEAD.json", 1, 1), ("atomic_write:index.json", 1, 2)]
    # journal: staging, swapping, done×4, swapped
    + [("atomic_write:journal.json", k, 2) for k in range(1, 8)]
    + [(f"stage:{n}", 1, 2) for n in (1, 2, 3)]
    + [(f"replace:stash:{n}", 1, 2) for n in (0, 1, 2)]
    + [(f"replace:swap:{n}", 1, 2) for n in (1, 2, 3)]
    + [("atomic_write:HEAD.json", 2, 0), ("atomic_write:index.json", 2, 0), ("txn_cleanup", 1, 0),
       ("remove:journal.json", 1, 0), ("append_jsonl:oplog.jsonl", 1, 0)]
)


@pytest.mark.parametrize("stage,count,expected", GOTO_STAGES)
def test_r2_kill_during_move(workdir, driver, stage, count, expected):
    build(workdir)
    driver(workdir, stage, count, "goto", "0")
    assert check_converged(workdir).at == expected


@pytest.mark.parametrize(
    "first,second",
    [
        (("replace:swap:3", 1), ("replace:unswap:2", 1)),
        (("replace:swap:3", 1), ("replace:unstash:1", 1)),
        (("replace:swap:3", 1), ("atomic_write:journal.json", 1)),
        (("replace:swap:3", 1), ("txn_cleanup", 1)),
        (("replace:swap:3", 1), ("remove:journal.json", 1)),
        (("atomic_write:HEAD.json", 2), ("atomic_write:HEAD.json", 1)),
        (("atomic_write:HEAD.json", 2), ("atomic_write:index.json", 1)),
        (("atomic_write:HEAD.json", 2), ("txn_cleanup", 1)),
    ],
)
def test_r3_kill_during_recover(workdir, driver, first, second):
    build(workdir)
    driver(workdir, *first, "goto", "0")
    driver(workdir, *second, "open")
    head = check_converged(workdir)
    assert head.at == (0 if first[0] == "atomic_write:HEAD.json" else 2)


COMMIT_STAGES = [
    ("chunk_write", 1),
    (r"atomic_write:[0-9a-f]{64}\.json", 1),
    ("atomic_write:counters.json", 1),
    (r"atomic_write:\d+\.json", 1),
    ("atomic_write:HEAD.json", 1),
    ("atomic_write:index.json", 1),
    ("append_jsonl:oplog.jsonl", 1),
]


@pytest.mark.parametrize("stage,count", COMMIT_STAGES)
def test_r1_kill_during_commit(workdir, driver, stage, count):
    build(workdir)
    driver(workdir, stage, count, "commit", "edit")
    with Repo.open(workdir) as repo:
        repo.commit("retry")
        assert not repo.work_state().dirty
        assert repo._store.verify_all().ok
        head = repo._history.head()
        assert repo._history.get(head.at).tree.keys() == EDITED.keys()
        ids = [e.id for e in repo.log()]
        assert ids == sorted(ids, reverse=True) and ids[-1] == 0
        repo.goto("0")
        assert files(workdir) == V0
        repo.goto(str(head.at))
        assert files(workdir) == EDITED
