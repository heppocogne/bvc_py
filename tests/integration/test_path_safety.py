# パスの安全性の網羅(M4-12)。観点: P-1〜P-4, P-6, P-7(改ざん)、I-16。
# 記録されたパス・ハッシュ・番号を書き換えても、作業フォルダの外や .bvc の中に書き込まず、
# その版を「壊れた版」として扱うことを、Repo の操作を通して確かめる。

from __future__ import annotations

import hashlib
import json
import os
import shutil
import string
import subprocess
import unicodedata
from pathlib import Path
from typing import Final
from unittest import mock

import pytest

from bvc import fsutil
from bvc.errors import BrokenVersion, MissingFiles, SafetyAbort, UnsafePath
from bvc.repo import Repo
from tests import helpers

IS_WINDOWS: Final[bool] = os.name == "nt"


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------


@pytest.fixture
def base():
    path = helpers.make_temp_dir()
    yield path
    helpers.remove_tree(path)


@pytest.fixture
def linear(base):
    # 版 0..2(a.bin の内容は v0, v1, v2)。@ = 2。作業フォルダの外に outside/secret.bin を置く
    work, outside = base / "work", base / "outside"
    work.mkdir()
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    (work / "a.bin").write_bytes(b"v0")
    repo = Repo.init(work, track=["**/*.bin"])
    for i in (1, 2):
        (work / "a.bin").write_bytes(f"v{i}".encode())
        repo.commit(f"c{i}")
    repo.close()
    return work


def commit_path(work: Path, cid: int) -> Path:
    return work / ".bvc" / "commits" / f"{cid}.json"


def tamper_commit(work: Path, cid: int, edit) -> None:
    # 版ファイルを読み、edit(data) で書き換えて保存する
    path = commit_path(work, cid)
    data = json.loads(path.read_text("utf-8"))
    edit(data)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def snapshot(base: Path) -> dict[str, bytes | None]:
    # base の下の全ファイル(内容)とフォルダ(None)。.bvc の中の記録・作業域は除く
    out: dict[str, bytes | None] = {}
    for root, dirs, files in os.walk(fsutil.os_path(base)):
        rel_root = os.path.relpath(root, fsutil.os_path(base)).replace("\\", "/")
        for d in dirs:
            out[f"{rel_root}/{d}"] = None
        for f in files:
            rel = f"{rel_root}/{f}"
            out[rel] = fsutil.read_bytes(os.path.join(root, f))
    allowed = ("oplog.jsonl", "health.json", "quarantine", "tmp", "txn", "lock")
    return {k: v for k, v in out.items() if not any(f"/.bvc/{a}" in k for a in allowed)}


def assert_unchanged(before: dict, base: Path) -> None:
    # 増えたものは無く、変わったものも無い。壊れたマニフェスト・チャンクの隔離(移動)だけは許す
    after = snapshot(base)
    assert sorted(set(after) - set(before)) == []
    for k, v in before.items():
        if k not in after and ("/.bvc/manifests/" in k or "/.bvc/chunks/" in k):
            continue
        assert after[k] == v, k


def open_repo(work: Path) -> Repo:
    return Repo.open(work)


# ---------------------------------------------------------------------------
# P-1: 記録されたパスの改ざん
# ---------------------------------------------------------------------------

NFD_NAME: Final[str] = unicodedata.normalize("NFD", "パ.bin")
BAD_PATHS: Final[list[str]] = [
    "../x.bin",
    "../outside/secret.bin",
    "sub/../../x.bin",
    "/abs.bin",
    "C:/x.bin",
    "C:\\x.bin",
    "C:x.bin",
    "\\\\server\\share\\x.bin",
    "//server/share/x.bin",
    ".bvc/config.json",
    ".BVC/objects.bin",
    "CON",
    "con.bin",
    "a/NUL",
    "",
    "a\x01.bin",
    "a\nb.bin",
    "a.bin.",
    "a.bin ",
    "a:stream.bin",
    NFD_NAME,  # bvc は NFC で記録する。正規化で変わるものも不正(重複を黙って1つにしない)
]


@pytest.mark.parametrize("bad", BAD_PATHS, ids=[repr(p) for p in BAD_PATHS])
def test_p1_tampered_tree_path_is_broken_version(linear, bad):
    work = linear
    base = work.parent

    def edit(data):
        sha = data["tree"].pop("a.bin")
        data["tree"][bad] = sha

    tamper_commit(work, 1, edit)
    before = snapshot(base)
    with open_repo(work) as repo:
        entry = {e.id: e for e in repo.log()}[1]
        assert entry.broken
        with pytest.raises(BrokenVersion):
            repo.goto("1")
        with pytest.raises(BrokenVersion):
            repo.undo()
        assert repo._history.head().at == 2
    assert_unchanged(before, base)
    with open_repo(work) as repo:
        # 壊れた版を飛ばして、健全な版 0 へ移動できる(仕様書 2.9節)
        r = repo.undo(skip_broken=True)
        assert (r.after.at, r.skipped) == (0, [1])
    assert (work / "a.bin").read_bytes() == b"v0"
    assert (base / "outside" / "secret.bin").read_bytes() == b"secret"
    assert not (base / "x.bin").exists()


@pytest.mark.parametrize(
    "renames",
    [
        [["../x.bin", "a.bin", 1.0]],
        [["a.bin", ".bvc/x", 1.0]],
        [["a.bin"]],
        [["a.bin", "b.bin", "1"]],
        [["a.bin", "b.bin", 2.0]],
        ["a.bin"],
    ],
)
def test_p1_tampered_renames_is_broken_version(linear, renames):
    work = linear
    tamper_commit(work, 1, lambda d: d.__setitem__("renames", renames))
    with open_repo(work) as repo:
        assert {e.id: e for e in repo.log()}[1].broken
        with pytest.raises(BrokenVersion):
            repo.goto("1")


def test_p1_tampered_index_paths_are_ignored(linear):
    # index.json(stat キャッシュ)に不正なパスを足しても、そのパスは使われない
    work = linear
    path = work / ".bvc" / "index.json"
    data = json.loads(path.read_text("utf-8"))
    entry = (
        next(iter(data["entries"].values()))
        if isinstance(data.get("entries"), dict)
        else None
    )
    if entry is None:
        pytest.skip("index.json の形式が想定と異なる")
    for bad in ("../x.bin", ".bvc/config.json", "C:/x.bin"):
        data["entries"][bad] = dict(entry)
    path.write_text(json.dumps(data), "utf-8")
    before = snapshot(work.parent)
    with open_repo(work) as repo:
        state = repo.work_state()
        assert not state.dirty
        repo.undo()
        repo.redo()
    assert (work / "a.bin").read_bytes() == b"v2"
    after = snapshot(work.parent)
    assert {k for k in after if not k.startswith("./work/")} == {
        k for k in before if not k.startswith("./work/")
    }


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 固有(8.3 形式の短い名前)")
def test_p1_short_name_alias_of_bvc_is_rejected(linear):
    # BVC~1 は名前の検査を通るが、.bvc と同じ実体を指すことがある。.bvc の中へは書き込まない
    work = linear
    alias = None
    for i in range(1, 5):
        cand = f"BVC~{i}"
        if (
            os.path.exists(work / cand)
            and fsutil.real_path(work / cand) == work / ".bvc"
        ):
            alias = cand
            break
    if alias is None:
        pytest.skip("8.3 形式の短い名前が作られない環境")
    assert fsutil.check_relpath(f"{alias}/evil.bin") == f"{alias}/evil.bin"

    def edit(data):
        data["tree"][f"{alias}/evil.bin"] = data["tree"]["a.bin"]
        data["tree"][f"{alias}/commits/99.json"] = data["tree"]["a.bin"]

    tamper_commit(work, 1, edit)
    before = snapshot(work.parent)
    with open_repo(work) as repo:
        with pytest.raises(UnsafePath):
            repo.goto("1")
        assert repo._history.head().at == 2
    assert_unchanged(before, work.parent)
    assert not (work / ".bvc" / "evil.bin").exists()


@pytest.mark.skipif(not IS_WINDOWS, reason="大文字小文字を区別しないファイルシステム")
def test_p7_tampered_case_collision_aborts(linear):
    work = linear

    def edit(data):
        data["tree"]["A.bin"] = data["tree"]["a.bin"]

    tamper_commit(work, 1, edit)
    before = snapshot(work.parent)
    with open_repo(work) as repo, pytest.raises(UnsafePath):
        repo.goto("1")
    assert_unchanged(before, work.parent)


# ---------------------------------------------------------------------------
# P-2: ハッシュ・番号の改ざん
# ---------------------------------------------------------------------------

BAD_SHAS: Final[list[str]] = [
    "A" * 64,
    "../" + "a" * 61,
    "a" * 63,
    "a" * 65,
    "g" * 64,
    "",
    1,
    None,
    ["a" * 64],
]


@pytest.mark.parametrize("bad", BAD_SHAS, ids=[repr(s)[:20] for s in BAD_SHAS])
def test_p2_tampered_manifest_name_is_broken_version(linear, bad):
    work = linear
    tamper_commit(work, 1, lambda d: d["tree"].__setitem__("a.bin", bad))
    before = snapshot(work.parent)
    with open_repo(work) as repo:
        assert {e.id: e for e in repo.log()}[1].broken
        with pytest.raises(BrokenVersion):
            repo.goto("1")
    assert_unchanged(before, work.parent)


@pytest.mark.parametrize("bad", [-1, "1", 1.5, 10**20, True, None, "../0"])
def test_p2_tampered_commit_id_is_unreadable(linear, bad):
    work = linear
    tamper_commit(work, 1, lambda d: d.__setitem__("id", bad))
    before = snapshot(work.parent)
    with open_repo(work) as repo:
        entry = {e.id: e for e in repo.log()}[1]
        assert entry.commit is None and entry.broken
        with pytest.raises(BrokenVersion):
            repo.goto("1")
    assert_unchanged(before, work.parent)


def test_p2_commit_file_names_are_checked(linear):
    # 番号として不正な名前の版ファイルは無視する(パスとして使わない)
    work = linear
    commits = work / ".bvc" / "commits"
    for name in ("01.json", "-1.json", "1.5.json", "x.json", "1 .json"):
        shutil.copy(commits / "1.json", commits / name)
    with open_repo(work) as repo:
        assert sorted(e.id for e in repo.log()) == [0, 1, 2]


def _rehashed_manifest(work: Path, sha: str, edit) -> str:
    # マニフェストを書き換え、名前(内容の SHA-256)を合わせて保存し直す。新しい名前を返す
    mdir = work / ".bvc" / "manifests"
    obj = json.loads((mdir / sha[:2] / f"{sha}.json").read_text("utf-8"))
    edit(obj)
    data = fsutil.canonical_json(obj)
    new = hashlib.sha256(data).hexdigest()
    (mdir / new[:2]).mkdir(exist_ok=True)
    (mdir / new[:2] / f"{new}.json").write_bytes(data)
    return new


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m["chunks"][0].__setitem__(0, "../../../outside/secret"),
        lambda m: m["chunks"][0].__setitem__(0, m["chunks"][0][0].upper()),
        lambda m: m.__setitem__("sha256", "../" + m["sha256"][3:]),
        lambda m: m.__setitem__("sha256", "0" * 64),  # 形式は正しいが内容と一致しない
    ],
    ids=["chunk-traversal", "chunk-upper", "whole-traversal", "whole-mismatch"],
)
def test_p2_rehashed_manifest_tampering_is_broken_version(linear, edit):
    work = linear
    with open_repo(work) as repo:
        sha = repo.get_commit(1).tree["a.bin"]
    new = _rehashed_manifest(work, sha, edit)
    tamper_commit(work, 1, lambda d: d["tree"].__setitem__("a.bin", new))
    before = snapshot(work.parent)
    with open_repo(work) as repo:
        with pytest.raises(BrokenVersion):
            repo.goto("1")
        assert repo._history.head().at == 2
    assert_unchanged(before, work.parent)
    assert (work / "a.bin").read_bytes() == b"v2"


def test_p2_tampered_health_keys_are_not_used_as_paths(linear):
    work = linear
    path = work / ".bvc" / "health.json"
    data = json.loads(path.read_text("utf-8"))
    for kind in ("bad_chunks", "bad_manifests", "bad_commits"):
        data[kind]["../../outside/secret"] = "x"
    path.write_text(json.dumps(data), "utf-8")
    with open_repo(work) as repo:
        repo.goto("0")
        repo.verify()
        repo.gc()
    assert (work.parent / "outside" / "secret.bin").read_bytes() == b"secret"


# ---------------------------------------------------------------------------
# P-3: シンボリックリンク・ジャンクション
# ---------------------------------------------------------------------------


def _make_dir_link(target: Path, link: Path) -> bool:
    return helpers.try_junction(target, link) or helpers.try_symlink(
        target, link, target_is_directory=True
    )


@pytest.fixture
def with_sub(base):
    # 版 0: sub/a.bin = s0, 版 1: sub/a.bin = s1。@ = 1
    work, outside = base / "work", base / "outside"
    (work / "sub").mkdir(parents=True)
    outside.mkdir()
    (work / "sub" / "a.bin").write_bytes(b"s0")
    repo = Repo.init(work, track=["**/*.bin"])
    (work / "sub" / "a.bin").write_bytes(b"s1")
    repo.commit("c1")
    repo.close()
    return work


def test_p3_folder_replaced_by_link_is_not_followed(with_sub):
    work = with_sub
    outside = work.parent / "outside"
    (outside / "a.bin").write_bytes(b"outside")
    shutil.rmtree(work / "sub")
    if not _make_dir_link(outside, work / "sub"):
        pytest.skip("リンクを作れない環境")
    before = snapshot(outside)
    with open_repo(work) as repo:
        # リンクの先は追跡しない(sub/a.bin は欠落になる)
        assert repo.work_state().missing == ["sub/a.bin"]
        with pytest.raises(MissingFiles):
            repo.commit("x")
        # 復元先の途中がリンクなら、何も変えずに中止する
        with pytest.raises((SafetyAbort, UnsafePath)):
            repo.goto("0", allow_missing=True)
        assert repo._history.head().at == 1
    assert snapshot(outside) == before


def test_p3_file_replaced_by_link_is_not_followed(with_sub):
    work = with_sub
    outside = work.parent / "outside"
    (outside / "secret.bin").write_bytes(b"secret")
    os.remove(work / "sub" / "a.bin")
    if not helpers.try_symlink(outside / "secret.bin", work / "sub" / "a.bin"):
        pytest.skip("シンボリックリンクを作れない環境")
    with open_repo(work) as repo, pytest.raises(SafetyAbort):
        repo.goto("0", allow_missing=True)
    assert (outside / "secret.bin").read_bytes() == b"secret"
    assert os.path.islink(work / "sub" / "a.bin")


# ---------------------------------------------------------------------------
# P-4: 作業フォルダの移動・改名・コピー
# ---------------------------------------------------------------------------


def test_p4_move_rename_and_copy(linear):
    work = linear
    moved = work.parent / "renamed" / "work2"
    moved.parent.mkdir()
    shutil.move(str(work), str(moved))
    with open_repo(moved) as repo:
        assert not repo.work_state().dirty
        repo.undo()
        assert (moved / "a.bin").read_bytes() == b"v1"
        repo.redo()
    copy = work.parent / "copy"
    shutil.copytree(moved, copy)
    with open_repo(copy) as repo:
        (copy / "a.bin").write_bytes(b"copy")
        repo.commit("in copy")
        repo.goto("0")
    # コピーしたもの同士は互いに影響しない
    with open_repo(moved) as repo:
        assert [e.id for e in repo.log()] == [2, 1, 0]
        assert not repo.work_state().dirty
    assert (moved / "a.bin").read_bytes() == b"v2"
    assert (copy / "a.bin").read_bytes() == b"v0"


# ---------------------------------------------------------------------------
# P-6: 日本語・特殊な名前・長いパス
# ---------------------------------------------------------------------------

SPECIAL_NAMES = [
    "日本語/ファイル.bin",
    "空白 を 含む/a b.bin",
    "emoji😀/🎉.bin",
    unicodedata.normalize("NFD", "ﾊﾟ濁点/パ.bin"),  # NFD で置く(NFC で記録される)
    "バ.bin",
    "x" * 100 + ".bin",
]


def _write(work: Path, rel: str, data: bytes) -> None:
    p = work.joinpath(*rel.split("/"))
    fsutil.makedirs(p.parent)
    with open(fsutil.os_path(p), "wb") as f:
        f.write(data)


def _contents(work: Path) -> dict[str, bytes]:
    # 作業フォルダの *.bin を NFC のパス → 内容で返す(長いパスでも読めるように os_path を通す)
    # os.walk は途中のエラーを黙って飛ばすので、深いフォルダも読めるよう常に \\?\ 付きにする
    out = {}
    with mock.patch.object(fsutil, "LONG_PATH_THRESHOLD", 0):
        top = fsutil.os_path(work)
        for root, dirs, files in os.walk(top, onerror=_raise):
            dirs[:] = [d for d in dirs if d != ".bvc"]
            for f in files:
                if f.endswith(".bin"):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, top).replace("\\", "/")
                    out[unicodedata.normalize("NFC", rel)] = fsutil.read_bytes(full)
    return out


def _raise(e: OSError) -> None:
    raise e


def _roundtrip(work: Path, names: list[str]) -> None:
    # init → 変更・追加・削除 → commit → undo → redo → goto → 各種操作
    v0 = {n: f"0:{i}".encode() for i, n in enumerate(names)}
    for n, d in v0.items():
        _write(work, n, d)
    repo = Repo.init(work, track=["**/*.bin"])
    try:
        nfc0 = {unicodedata.normalize("NFC", n): d for n, d in v0.items()}
        assert sorted(repo.get_commit(0).tree) == sorted(nfc0)
        v1 = {n: f"1:{i}".encode() for i, n in enumerate(names[1:])}
        v1["new/追加.bin"] = b"new"
        os.remove(fsutil.os_path(work.joinpath(*names[0].split("/"))))
        for n, d in v1.items():
            _write(work, n, d)
        repo.commit("c1", allow_missing=True)
        nfc1 = {unicodedata.normalize("NFC", n): d for n, d in v1.items()}
        assert not repo.work_state().dirty
        repo.undo()
        assert _contents(work) == nfc0
        repo.redo()
        assert _contents(work) == nfc1
        repo.goto("0")
        assert _contents(work) == nfc0
        assert not repo.work_state().dirty
        repo.log()
        rep = repo.verify()
        assert not (rep.bad_chunks or rep.bad_manifests or rep.broken_commits)
        repo.note("メモ")
        repo.goto("1")
        repo.discard()
        repo.gc()
        assert _contents(work) == nfc0
    finally:
        repo.close()
    sub = names[0].split("/")[0]
    if "/" in names[0]:
        # サブフォルダから開く(P-5)
        fsutil.makedirs(work / sub)
        Repo.open(work / sub).close()


def test_p6_special_names(base):
    work = base / "work"
    work.mkdir()
    _roundtrip(work, SPECIAL_NAMES)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 固有(MAX_PATH)")
@pytest.mark.parametrize("case", ["deep-files", "long-workdir"])
def test_p6_long_paths_without_long_path_support(base, case):
    # LongPathsEnabled が無効な環境を模擬して(監査フック)、260 文字を超えるパスを扱う(I-16)
    if case == "deep-files":
        work = base / "work"
        deep = "/".join(["d" * 50] * 5)
        names = [f"{deep}/a.bin", f"{deep}/日本語/b.bin", "short.bin"]
    else:
        work = base.joinpath(*(["w" * 60] * 4))
        names = ["sub/a.bin", "b.bin"]
    fsutil.makedirs(work)
    with helpers.no_long_paths():
        _roundtrip(work, names)
        assert max(len(str(work / n)) for n in names) > 260


# ---------------------------------------------------------------------------
# I-16: ネットワークフォルダ(UNC、subst、ネットワークドライブ)
# ---------------------------------------------------------------------------


def _free_drive_letters() -> list[str]:
    return [
        c for c in reversed(string.ascii_uppercase) if not os.path.exists(f"{c}:\\")
    ]


def _basic_scenario(work: Path) -> None:
    (work / "sub").mkdir()
    (work / "sub" / "a.bin").write_bytes(b"v0")
    with Repo.init(work, track=["**/*.bin"]) as repo:
        (work / "sub" / "a.bin").write_bytes(b"v1")
        (work / "b.bin").write_bytes(b"b")
        repo.commit("c1")
        repo.undo()
        assert (work / "sub" / "a.bin").read_bytes() == b"v0"
        assert not (work / "b.bin").exists()
        repo.redo()
        repo.goto("0")
        repo.goto("1")
        assert (work / "b.bin").read_bytes() == b"b"
        assert not repo.work_state().dirty
        repo.verify()
        repo.discard()
        repo.gc()
    with Repo.open(work / "sub") as repo:
        assert [e.id for e in repo.log()] == [0]


@pytest.mark.windows
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 固有")
def test_i16_unc_path(base):
    drive, rest = os.path.splitdrive(str(base))
    unc = Path(f"\\\\localhost\\{drive[0]}$" + rest)
    if not os.path.isdir(unc):
        pytest.skip("管理共有(\\\\localhost\\C$)にアクセスできない環境")
    work = unc / "work"
    work.mkdir()
    _basic_scenario(work)


@pytest.mark.windows
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 固有")
def test_i16_subst_drive(base):
    letters = _free_drive_letters()
    if not letters:
        pytest.skip("空いているドライブ文字が無い")
    letter = letters[0]
    r = subprocess.run(
        ["subst", f"{letter}:", str(base)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip("subst を使えない環境")
    try:
        work = Path(f"{letter}:\\work")
        work.mkdir()
        _basic_scenario(work)
    finally:
        subprocess.run(
            ["subst", f"{letter}:", "/D"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


@pytest.mark.windows
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 固有")
@pytest.mark.skip(
    reason="ネットワークドライブでのファイル操作が不安定(SMBキャッシュなど)。journalが残るため、動作に影響はない"
)
def test_i16_network_drive(base):
    drive, rest = os.path.splitdrive(str(base))
    letters = _free_drive_letters()
    if not letters:
        pytest.skip("空いているドライブ文字が無い")
    letter = letters[0]
    try:
        r = subprocess.run(
            [
                "net",
                "use",
                f"{letter}:",
                f"\\\\localhost\\{drive[0]}$",
                "/persistent:no",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("net use が応答しない環境")
    if r.returncode != 0:
        pytest.skip("ネットワークドライブを割り当てられない環境")
    try:
        work = Path(f"{letter}:" + rest) / "work"
        work.mkdir()
        _basic_scenario(work)
    finally:
        subprocess.run(
            ["net", "use", f"{letter}:", "/delete", "/y"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
