# git 連携の結合テスト(M6)。観点: G-1〜G-10, F-1(sync), P-1(bvc.lock), R-1(bvc.lock の書き込み)。
# 実際の git を使う。フックは、テストを実行している Python で bvc を起動するように設置する。

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import bvc
from bvc import cli, gitlink
from bvc.errors import GitFailed, PinnedCommit
from bvc.model import Head
from bvc.repo import Repo
from tests import helpers

pytestmark = [
    pytest.mark.git,
    pytest.mark.skipif(shutil.which("git") is None, reason="git がありません"),
]

SRC = Path(bvc.__file__).resolve().parents[1]
V1, V2, V3 = b"one" * 100, b"two" * 100, b"three" * 100


class GitRepo:
    # テスト用の git リポジトリ兼 bvc の作業フォルダ。
    def __init__(self, root: Path):
        self.root = root
        pythonpath = os.pathsep.join(
            p for p in (str(SRC), os.environ.get("PYTHONPATH")) if p
        )
        self.env = dict(os.environ, PYTHONPATH=pythonpath, PYTHONIOENCODING="utf-8")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cp = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            stdin=subprocess.DEVNULL,  # pytest の標準入力の差し替えで、Windows ではハンドルが無効になるため
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and cp.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} が失敗しました\n{cp.stdout}\n{cp.stderr}"
            )
        return cp

    def bvc(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(["-C", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def write(self, rel: str, data: bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def lock(self) -> gitlink.LockFile | None:
        return gitlink.read_lock(self.root / "bvc.lock")

    def open(self) -> Repo:
        return Repo.open(self.root)

    def tree(self, commit_id: int) -> dict[str, str]:
        with self.open() as repo:
            return repo.get_commit(commit_id).tree

    def head(self) -> Head:
        with self.open() as repo:
            return repo._history.head()

    def set_config(self, **git: object) -> None:
        path = self.root / ".bvc" / "config.json"
        data = json.loads(path.read_text("utf-8"))
        data["git"].update(git)
        path.write_text(json.dumps(data), encoding="utf-8")

    def install_hooks(self) -> None:
        cmd = f'"{Path(sys.executable).as_posix()}" -m bvc'
        with self.open() as repo:
            gitlink.install_hooks(
                repo._git.hooks_dir(), repo._git.workdir_prefix(), cmd
            )

    def git_commit(self, msg: str, *add: str) -> subprocess.CompletedProcess:
        self.git("add", *(add or ("-A",)))
        return self.git("commit", "-q", "-m", msg)


def make_repo(root: Path, hooks: bool = True) -> GitRepo:
    # a.bin(V1)と code.txt を版 0 / 最初の git コミットにする。
    g = GitRepo(root)
    g.git("init", "-q", "-b", "main")
    g.git("config", "user.email", "t@example.com")
    g.git("config", "user.name", "t")
    g.git("config", "core.autocrlf", "false")
    g.write(".gitignore", b"/.bvc/\n*.bin\n")
    g.write("code.txt", b"code1\n")
    g.write("a.bin", V1)
    Repo.init(root, track=["*.bin"], git=True).close()
    if hooks:
        g.install_hooks()
    g.git_commit("c1")
    return g


@pytest.fixture
def g(workdir: Path) -> GitRepo:
    return make_repo(workdir)


@pytest.fixture
def g_nohooks(workdir: Path) -> GitRepo:
    return make_repo(workdir, hooks=False)


def sha(data: bytes) -> str:
    return helpers.sha256_bytes(data)


# --- G-1: bvc.lock の更新 ---


def test_g1_lock_follows_head(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    lock = g.lock()
    assert lock.bvc_commit == 0 and lock.files["a.bin"].sha256 == sha(V1)
    assert json.loads(g.read(".bvc/lockstate.json"))["tree_hash"] == gitlink.tree_hash(
        lock.tree
    )

    g.write("a.bin", V2)
    assert g.bvc("commit", "-m", "v2")[0] == 0
    lock = g.lock()
    assert lock.bvc_commit == 1 and lock.files["a.bin"].sha256 == sha(V2)
    assert lock.tree == g.tree(1)
    after_commit = g.read("bvc.lock")

    assert g.bvc("undo")[0] == 0
    assert g.lock().bvc_commit == 0
    assert g.bvc("goto", "1")[0] == 0
    assert g.read("bvc.lock") == after_commit  # 同じ版なら同じバイト列(差分が出ない)
    assert g.bvc("--json", "log")[1].count('"lock_status": "ok"') == 1


def test_r1_lock_write_failure_is_repaired_on_open(
    g_nohooks: GitRepo, monkeypatch
) -> None:
    # R-1: HEAD の更新後、bvc.lock を書く前に失敗しても、次に開いたときに書き直す
    g = g_nohooks
    g.write("a.bin", V2)

    def fail(*args, **kw):
        raise OSError("書き込み失敗")

    with monkeypatch.context() as m:
        m.setattr(gitlink, "write_lock", fail)
        code, _, err = g.bvc("commit")
    assert code == 0 and "更新できませんでした" in err
    assert g.lock().bvc_commit == 0
    code, _, err = g.bvc("log")
    assert code == 0 and "非同期" not in err
    assert g.lock().bvc_commit == 1


# --- G-2: checkout 連動 ---


def test_g2_checkout_restores_binaries(g: GitRepo) -> None:
    g.write("a.bin", V2)
    g.write("code.txt", b"code2\n")
    g.git_commit("c2")  # pre-commit で自動コミット(版 1)
    assert g.lock().files["a.bin"].sha256 == sha(V2)

    g.write("a.bin", V3)  # 未コミットの変更
    cp = g.git("checkout", "-q", "HEAD~1")
    assert g.read("a.bin") == V1
    assert "版0に移動しました" in cp.stdout + cp.stderr
    assert "上書き" not in cp.stderr and "非同期" not in cp.stderr
    assert (
        g.git("status", "--porcelain", "bvc.lock").stdout == ""
    )  # bvc.lock は git の内容のまま
    with g.open() as repo:
        auto = [e.commit for e in repo.log() if e.commit.message == "auto: before sync"]
        assert len(auto) == 1
        # 未コミットの変更(V3)は自動コミットで保存されている
        assert repo._store.get_manifest(auto[0].tree["a.bin"]).sha256 == sha(V3)

    g.git("checkout", "-q", "main")
    assert g.read("a.bin") == V2
    assert g.git("status", "--porcelain", "bvc.lock").stdout == ""
    assert g.bvc("log")[2] == ""  # 非同期状態の警告が無い


# --- G-3: pre-commit ---


def test_g3_pre_commit_snapshot_and_pin(g: GitRepo) -> None:
    g.write("a.bin", V2)
    g.write("code.txt", b"code2\n")
    g.git_commit("c2", "code.txt", "bvc.lock")
    committed = gitlink.parse_lock(
        g.git("show", "HEAD:bvc.lock").stdout.encode("utf-8")
    )
    with g.open() as repo:
        head = repo._history.head()
        c = repo.get_commit(head.at)
        assert c.message == "auto: git pre-commit"
        assert committed.tree == c.tree
        pins = repo._history.pins()
    git_head = g.git("rev-parse", "HEAD").stdout.strip()
    assert [(p.git, p.bvc) for p in pins][-1] == (git_head, head.at)
    # pin された版は discard で警告して中止(--force で実行できる)
    with g.open() as repo:
        repo.goto("0")
        with pytest.raises(PinnedCommit):
            repo.discard(str(head.at))


def test_g3_pre_commit_reject(g: GitRepo) -> None:
    g.set_config(pre_commit="reject")
    before = g.git("rev-parse", "HEAD").stdout
    g.write("a.bin", V2)
    g.write("code.txt", b"code2\n")
    g.git("add", "code.txt")
    cp = g.git("commit", "-q", "-m", "c2", check=False)
    assert cp.returncode != 0
    assert "未コミットの変更" in cp.stderr
    assert g.git("rev-parse", "HEAD").stdout == before
    assert g.head().at == 0  # 自動コミットも作らない


def test_g3_unstaged_lock_warning(g: GitRepo) -> None:
    g.write("a.bin", V2)
    assert g.bvc("commit")[0] == 0
    g.write("code.txt", b"code2\n")
    g.git("add", "code.txt")
    cp = g.git("commit", "-q", "-m", "c2")
    assert "ステージされていません" in cp.stderr


# --- G-4: gc の保護 ---


def _history_only_setup(g: GitRepo) -> str:
    # 版 0 は git の履歴(c1)の bvc.lock からだけ参照される状態にする(pin も無い)。
    g.write("a.bin", V2)
    g.bvc("commit")
    g.git_commit("c2")
    c1 = g.git("rev-parse", "HEAD~1").stdout.strip()
    assert g.bvc("discard", "0")[0] == 0
    return c1


def test_g4_gc_keeps_data_referenced_by_git_history(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    c1 = _history_only_setup(g)
    manifest_v1 = g.tree(0)["a.bin"]
    code, out, _ = g.bvc("--json", "gc")
    assert code == 0 and json.loads(out)["deleted_commits"] == [0]
    with g.open() as repo:
        assert repo._store.manifest_ok(manifest_v1)
    # 消した版の内容に、git の checkout と sync で戻れる(import の版を作る)
    g.git("checkout", "-q", c1)
    code, out, _ = g.bvc("--json", "sync")
    assert code == 0, out
    res = json.loads(out)
    assert res["imported"]["kind"] == "import" and res["imported"]["parent"] == 1
    assert g.read("a.bin") == V1


def test_g4_gc_no_git_and_g5_sync_after_gc(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    c1 = _history_only_setup(g)
    assert g.bvc("gc", "--no-git")[0] == 0
    g.git("checkout", "-q", c1)
    before = helpers.tree_hashes(g.root)
    code, _, err = g.bvc("sync")
    assert code == 1 and "データがリポジトリにありません" in err
    assert helpers.tree_hashes(g.root) == before  # 作業ファイルは一切変わらない
    code, _, err = g.bvc("log")
    assert code == 0 and "非同期状態" in err


def test_g4_git_failure_deletes_nothing(g_nohooks: GitRepo, monkeypatch) -> None:
    g = g_nohooks
    _history_only_setup(g)
    monkeypatch.setenv("PATH", "")
    with g.open() as repo, pytest.raises(GitFailed):
        repo.gc()
    assert (g.root / ".bvc" / "commits" / "0.json").exists()


def test_g4_unreadable_lock_in_history_deletes_nothing(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    _history_only_setup(g)
    g.write("bvc.lock", b"{broken")
    g.git("add", "bvc.lock")
    g.git("commit", "-q", "-m", "broken")
    g.git("checkout", "-q", "HEAD~1", "--", "bvc.lock")
    code, _, err = g.bvc("gc")
    assert code == 3 and "gcを中止" in err
    assert (g.root / ".bvc" / "commits" / "0.json").exists()


# --- G-5: データが無い・不正な bvc.lock での sync ---


def _write_lock(g: GitRepo, files: dict, commit: int | None = 0) -> None:
    g.write(
        "bvc.lock",
        json.dumps({"format": 1, "bvc_commit": commit, "files": files}).encode(),
    )


@pytest.mark.parametrize(
    "case",
    [
        "unknown_manifest",
        "partial",
        "hand_edited",
        "unsafe_path",
        "unknown_format",
        "broken_json",
    ],
)
def test_g5_sync_changes_nothing(g_nohooks: GitRepo, case: str) -> None:
    g = g_nohooks
    good = g.lock().files["a.bin"]
    entry = {"size": good.size, "sha256": good.sha256, "manifest": good.manifest}
    missing = {"size": 1, "sha256": "c" * 64, "manifest": "d" * 64}
    if case == "unknown_manifest":
        _write_lock(g, {"a.bin": missing}, commit=99)
    elif case == "partial":
        _write_lock(g, {"a.bin": entry, "b.bin": missing})
    elif case == "hand_edited":
        _write_lock(g, {"other.bin": {**entry, "size": 1}})
    elif case == "unsafe_path":
        _write_lock(g, {"../evil.bin": entry})
    elif case == "unknown_format":
        g.write("bvc.lock", json.dumps({"format": 2, "files": {}}).encode())
    else:
        g.write("bvc.lock", b"{")
    before = helpers.tree_hashes(g.root)
    lock_before = g.read("bvc.lock")
    code, _, err = g.bvc("sync")
    assert code == 1, err
    assert helpers.tree_hashes(g.root) == before
    assert g.read("bvc.lock") == lock_before
    assert not (g.root.parent / "evil.bin").exists()
    assert g.head().at == 0
    assert "非同期状態" in g.bvc("log")[2]
    assert json.loads(g.bvc("--json", "log")[1])["lock_status"] == "out_of_sync"


def test_g5_overwriting_out_of_sync_lock_is_logged(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    _write_lock(
        g, {"a.bin": {"size": 1, "sha256": "c" * 64, "manifest": "d" * 64}}, commit=99
    )
    g.write("a.bin", V2)
    code, _, err = g.bvc("commit")
    assert code == 0 and "上書きします" in err
    assert g.lock().bvc_commit == 1
    oplog = (g.root / ".bvc" / "oplog.jsonl").read_text("utf-8").splitlines()
    rec = [json.loads(line) for line in oplog if '"lock_overwrite"' in line]
    assert len(rec) == 1 and '"bvc_commit": 99' in rec[0]["previous"]


def test_sync_require_lock_and_missing_lock(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    (g.root / "bvc.lock").unlink()
    code, _, err = g.bvc("sync")
    assert code == 0 and "bvc.lockがありません" in err
    code, out, _ = g.bvc("--json", "sync")
    res = json.loads(out)
    assert (code, res["changed"], res["lock_found"]) == (0, False, False)
    assert g.bvc("sync", "--require-lock")[0] == 1
    assert g.read("a.bin") == V1


# --- G-8: 不正な bvc.lock のコミット拒否 ---


@pytest.mark.parametrize("case", ["unknown_manifest", "hand_edited", "broken"])
def test_g8_pre_commit_rejects_invalid_staged_lock(g: GitRepo, case: str) -> None:
    good = g.lock().files["a.bin"]
    if case == "unknown_manifest":
        _write_lock(g, {"a.bin": {"size": 1, "sha256": "c" * 64, "manifest": "d" * 64}})
    elif case == "hand_edited":
        _write_lock(
            g,
            {
                "a.bin": {
                    "size": good.size + 1,
                    "sha256": good.sha256,
                    "manifest": good.manifest,
                }
            },
        )
    else:
        g.write("bvc.lock", b"{")
    before = g.git("rev-parse", "HEAD").stdout
    g.git("add", "bvc.lock")
    cp = g.git("commit", "-q", "-m", "bad", check=False)
    assert cp.returncode != 0
    assert "git commitを中止します" in cp.stderr
    assert g.git("rev-parse", "HEAD").stdout == before


# --- G-9: 番号と内容の食い違い ---


def test_g9_content_wins_over_commit_number(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    g.write("a.bin", V2)
    g.bvc("commit")  # 版 1
    tree1 = g.lock().tree
    g.bvc("undo")
    with g.open() as repo:
        ms = {s: repo._store.get_manifest(s) for s in tree1.values()}
    g.write("bvc.lock", gitlink.lock_bytes(0, tree1, ms))  # 番号は 0、内容は版 1
    lock_bytes = g.read("bvc.lock")
    code, _, err = g.bvc("sync")
    assert code == 0 and "一致しません" in err
    assert g.head().at == 1 and g.read("a.bin") == V2
    assert g.read("bvc.lock") == lock_bytes  # 内容が同じなので書き換えない


def test_sync_imports_when_only_discarded_version_matches(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    g.write("a.bin", V2)
    g.bvc("commit")  # 版 1
    lock1 = g.read("bvc.lock")
    g.bvc("undo")
    g.bvc("discard", "1")
    g.write("bvc.lock", lock1)
    g.write("a.bin", V3)  # 未コミットの変更は自動コミットで残る
    code, out, _ = g.bvc("--json", "sync")
    res = json.loads(out)
    assert code == 0 and res["changed"]
    assert res["auto_commit"]["message"] == "auto: before sync"
    assert res["imported"]["parent"] == res["auto_commit"]["id"]
    assert res["after"]["at"] == res["imported"]["id"]
    assert g.read("a.bin") == V2
    assert g.read("bvc.lock") == lock1
    assert g.bvc("sync")[1].startswith("変更なし")


# --- G-6: 既存フック ---


def test_g6_existing_hook_is_not_overwritten(g_nohooks: GitRepo) -> None:
    g = g_nohooks
    hooks = g.root / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    code, out, err = g.bvc("git", "install-hooks")
    assert code == 0
    assert (hooks / "pre-commit").read_text(
        encoding="utf-8"
    ) == "#!/bin/sh\necho mine\n"
    assert "下記の処理を追加して下さい" in err and "git pre-commit || exit $?" in err
    assert (hooks / "post-commit").exists() and (hooks / "post-checkout").exists()
    code, out, _ = g.bvc("--json", "git", "install-hooks")
    res = json.loads(out)
    assert (res["changed"], res["already"], list(res["manual"])) == (
        False,
        ["post-commit", "post-checkout"],
        ["pre-commit"],
    )


def test_hooks_survive_moving_and_copying_the_folder(
    g: GitRepo, tmp_path: Path
) -> None:
    # N-50: フックは作業フォルダの絶対パスを持たない。移動・複製後も、そのフォルダ自身を操作する
    for name in gitlink.HOOK_NAMES:
        text = (g.root / ".git" / "hooks" / name).read_text(encoding="utf-8")
        assert " -C " not in text and str(g.root.as_posix()) not in text
    copy = tmp_path / "copy"
    shutil.copytree(g.root, copy)
    c = GitRepo(copy)
    c.write("a.bin", V2)
    c.write("code.txt", b"code2\n")
    assert (
        c.git_commit("c2").returncode == 0
    )  # コピー側の pre-commit が、コピー側の bvc を動かす
    assert c.head().at == 1
    assert g.head().at == 0  # 元のフォルダは変わらない
    assert g.read("a.bin") == V1


def test_hooks_in_subfolder_use_relative_dir(workdir: Path) -> None:
    # bvc の作業フォルダが git の作業ツリーの最上位でなければ、最上位からの相対パスで -C を付ける
    top = workdir
    sub = top / "sub" / "dir"
    sub.mkdir(parents=True)
    GitRepo(top).git("init", "-q")
    g = GitRepo(sub)
    g.write("a.bin", V1)
    assert g.bvc("init", "--track", "*.bin", "--git")[0] == 0
    text = (top / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert ' -C "sub/dir" git pre-commit' in text


def test_install_hooks_fixes_legacy_absolute_dir(g_nohooks: GitRepo) -> None:
    # LEGACY-HOOK-C(削除予定): このテストごと削除する
    # 以前の版が書いた絶対パスの -C は、再実行で相対パス(無し)に直る。それ以外の内容は変えない
    g = g_nohooks
    hooks = g.root / ".git" / "hooks"
    legacy = f'python -m bvc -C "{g.root.as_posix()}" git'
    (hooks / "pre-commit").write_text(
        f"#!/bin/sh\n{gitlink.HOOK_MARK}\nexec {legacy} pre-commit\n", encoding="utf-8"
    )
    (hooks / "post-commit").write_text(
        f"#!/bin/sh\necho mine\n{legacy} pin\n", encoding="utf-8"
    )
    code, out, _ = g.bvc("--json", "git", "install-hooks")
    res = json.loads(out)
    assert code == 0
    assert (res["changed"], res["updated"], list(res["manual"])) == (
        True,
        ["pre-commit", "post-commit"],
        [],
    )
    assert (hooks / "pre-commit").read_text(encoding="utf-8") == (
        f"#!/bin/sh\n{gitlink.HOOK_MARK}\nexec python -m bvc git pre-commit\n"
    )
    assert (hooks / "post-commit").read_text(encoding="utf-8") == (
        "#!/bin/sh\necho mine\npython -m bvc git pin\n"
    )
    assert json.loads(g.bvc("--json", "git", "install-hooks")[1])["changed"] is False


# --- G-7: git が無い環境 ---


def test_g7_works_without_git(workdir: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    g = GitRepo(workdir)
    g.write("a.bin", V1)
    code, _, err = g.bvc("init", "--track", "*.bin", "--git")
    assert code == 1 and "git init" in err
    assert not (workdir / ".bvc").exists()
    assert g.bvc("init", "--track", "*.bin")[0] == 0
    g.write("a.bin", V2)
    for args in (
        ("commit",),
        ("undo",),
        ("redo",),
        ("discard", "0"),
        ("gc",),
        ("verify",),
        ("log",),
    ):
        code, out, err = g.bvc(*args)
        assert code == 0, (args, out, err)
    assert not (workdir / "bvc.lock").exists()
    assert g.bvc("sync")[0] == 1  # git 連携が無効
    for args in (
        ("git", "pin"),
        ("git", "pre-commit"),
        ("git", "post-checkout", "a", "b", "1"),
    ):
        assert g.bvc(*args)[0] == 0  # フック用のコマンドは何もしない


def test_init_git_installs_hooks_and_prints_gitignore(workdir: Path) -> None:
    g = GitRepo(workdir)
    g.git("init", "-q")
    g.write("a.bin", V1)
    code, out, err = g.bvc("init", "--track", "*.bin", "--git")
    assert code == 0, err
    assert "/.bvc/" in out and "*.bin" in out
    for name in gitlink.HOOK_NAMES:
        assert gitlink.HOOK_MARK in (workdir / ".git" / "hooks" / name).read_text(
            encoding="utf-8"
        )
    assert g.lock().bvc_commit == 0


# --- G-10: bvc.lock の無いコミットとの行き来 ---


def test_g10_lockless_commit_roundtrip(g: GitRepo) -> None:
    g.write("a.bin", V2)
    g.write("code.txt", b"code2\n")
    g.git_commit("c2")  # 版 1
    # bvc.lock の無いコミット(bvc 導入前・bvc.lock を削除したコミット)
    g.git("checkout", "-q", "--orphan", "nolock")
    g.git("rm", "-q", "--cached", "bvc.lock")
    (g.root / "bvc.lock").unlink()
    g.git("commit", "-q", "-m", "nolock")
    assert g.read("a.bin") == V2  # 作業ファイルはそのまま
    assert json.loads(g.bvc("--json", "log")[1])["lock_status"] == "missing"

    g.write("a.bin", V3)
    assert g.bvc("commit")[0] == 0  # 版 2
    assert not (
        g.root / "bvc.lock"
    ).exists()  # 作り直さない(checkout が拒否されないように)
    assert g.bvc("sync")[0] == 0

    cp = g.git("checkout", "-q", "main")  # 追跡されていない bvc.lock が無いので成功する
    assert g.read("a.bin") == V2
    assert g.head().at == 1
    assert "非同期" not in cp.stderr
    assert json.loads(g.bvc("--json", "log")[1])["lock_status"] == "ok"


# --- F-11: git 連携のコマンドの --json ---


def test_f11_json_of_git_commands(g: GitRepo) -> None:
    def json_cmd(*args: str) -> dict:
        code, out, err = g.bvc("--json", *args)
        assert (code, err) == (0, ""), out
        return json.loads(out)

    assert set(json_cmd("git", "install-hooks")) == {
        "changed",
        "hooks_dir",
        "installed",
        "already",
        "updated",  # LEGACY-HOOK-C(削除予定)
        "manual",
        "warnings",
    }
    g.write("a.bin", V2)
    data = json_cmd("git", "pre-commit")
    assert set(data) == {"changed", "auto_commit", "staged_ok", "warnings"}
    assert (
        data["changed"] and data["staged_ok"] and data["auto_commit"]["kind"] == "auto"
    )
    g.git("commit", "-q", "--no-verify", "-m", "c2")  # post-commit はフックで記録済み
    data = json_cmd("git", "pin")
    assert set(data) == {"changed", "pin", "warnings"}
    assert (data["changed"], data["pin"]["bvc"]) == (False, 1)  # 記録済み
    data = json_cmd("git", "post-checkout", "a", "b", "1")
    assert (
        set(data) == {"changed", "synced", "sync", "warnings"}
        and data["synced"] is False
    )
    data = json_cmd("sync")
    assert {"lock_found", "imported", "before", "after", "auto_commit"} <= set(data)


def test_post_checkout_failure_is_loud(g: GitRepo) -> None:
    # sync に失敗しても git checkout は成功する。目立つ警告を出す(仕様書 5.3節)
    g.write("a.bin", V2)
    g.write("code.txt", b"code2\n")
    g.git_commit("c2")
    head = g.head().at
    g.write(
        "bvc.lock",
        json.dumps(
            {
                "format": 1,
                "bvc_commit": 0,
                "files": {
                    "a.bin": {"size": 1, "sha256": "c" * 64, "manifest": "d" * 64}
                },
            }
        ).encode(),
    )
    g.git("add", "bvc.lock")
    g.git("commit", "-q", "--no-verify", "-m", "bad lock")
    g.git("checkout", "-q", "HEAD~1")
    cp = g.git("checkout", "-q", "main", check=False)
    # フックの終了コードが git checkout の終了コードになる(checkout 自体は完了している)
    assert cp.returncode == 1
    assert "=" * 60 in cp.stderr and "非同期状態" in cp.stderr
    assert g.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert g.read("a.bin") == V2 and g.head().at == head
