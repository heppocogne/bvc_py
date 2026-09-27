# bvc.lock・フック・git 履歴の走査(subprocess で git を呼ぶ)。設計書 1.1節・5節。
# repo から使う下位層。版の履歴(history)や保存データ(store)は知らない。

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import zipimport
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from . import fsutil
from .errors import CorruptData, GitFailed, UnsafePath
from .fsutil import (
    atomic_write,
    atomic_write_json,
    check_format,
    check_git_sha,
    check_id,
    check_relpath,
    check_sha,
)
from .model import HooksResult, LockEntry, LockFile, Manifest

logger = logging.getLogger(__name__)

LOCKSTATE: Final[str] = "lockstate.json"
HOOK_NAMES: Final[tuple[str, ...]] = ("pre-commit", "post-commit", "post-checkout")
# bvc が設置したフックの印(この行があれば設置済みとみなす)
HOOK_MARK: Final[str] = "# bvc: bvc git install-hooks が設置したフック"
_ZERO_SHA_RE: Final[re.Pattern[str]] = re.compile(r"0+")


# ---------------------------------------------------------------------------
# bvc.lock(仕様書 5.1節)
# ---------------------------------------------------------------------------


def tree_hash(tree: Mapping[str, str]) -> str:
    # tree(パス → マニフェスト sha256)を正規化した SHA-256。版の同一判定と pins に使う。
    return hashlib.sha256(fsutil.canonical_json(dict(tree))).hexdigest()


def lock_bytes(
    commit_id: int, tree: Mapping[str, str], manifests: Mapping[str, Manifest]
) -> bytes:
    # bvc.lock の内容。キーをソートし、インデント付き・末尾に改行(git の差分をファイル単位の行にするため)。
    files = {
        path: {
            "size": manifests[sha].size,
            "sha256": manifests[sha].sha256,
            "manifest": sha,
        }
        for path, sha in tree.items()
    }
    obj = {"format": 1, "bvc_commit": commit_id, "files": files}
    text = json.dumps(
        obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def parse_lock(data: bytes, what: str = "bvc.lock") -> LockFile:
    # bvc.lock を解析し、パス・ハッシュ・番号を検査する(仕様書 2.10節)。
    # 解析できない・値が不正なら CorruptData、パスが不正なら UnsafePath、知らない format なら UnsupportedFormat。
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise CorruptData(f"{what}: JSON として読めません") from e
    obj = check_format(obj, what)
    commit_id = obj.get("bvc_commit")
    files = obj.get("files")
    if not isinstance(files, dict):
        raise CorruptData(f"{what}: files がありません")
    try:
        if commit_id is not None:
            commit_id = check_id(commit_id)
    except UnsafePath:
        raise CorruptData(f"{what}: bvc_commit が不正です: {commit_id!r:.80}") from None
    entries: dict[str, LockEntry] = {}
    for path, e in files.items():
        # bvc は NFC で書くので、正規化で変わるパスも不正とする(history と同じ規則)
        if check_relpath(path) != path:
            raise UnsafePath(f"{what}: 正規化されていないパスです: {path!r}", path=path)
        try:
            if not isinstance(e, dict):
                raise UnsafePath("")
            size = e.get("size")
            if type(size) is not int or size < 0:
                raise UnsafePath("")
            entries[path] = LockEntry(
                size=size,
                sha256=check_sha(e.get("sha256")),
                manifest=check_sha(e.get("manifest")),
            )
        except UnsafePath:
            raise CorruptData(f"{what}: {path} の記録が不正です", path=path) from None
    return LockFile(bvc_commit=commit_id, files=entries)


def read_lock(path: Path, what: str = "bvc.lock") -> LockFile | None:
    # 作業フォルダの bvc.lock を読む。無ければ None。
    try:
        data = fsutil.read_bytes(path)
    except FileNotFoundError:
        return None
    return parse_lock(data, what)


def read_lock_raw(path: Path) -> bytes | None:
    try:
        return fsutil.read_bytes(path)
    except FileNotFoundError:
        return None


def write_lock(workdir: Path, lock_file: str, data: bytes, tmpdir: Path) -> None:
    # bvc.lock を原子的に書く。書き込み先は作業フォルダの中で、リンクをたどらないこと(仕様書 2.10節)。
    path = fsutil.resolve_in_workdir(workdir, lock_file)
    fsutil.makedirs(path.parent)
    atomic_write(path, data, tmpdir)


def read_lockstate(bvc_dir: Path) -> tuple[bool, str | None]:
    # (記録があるか, 最後に書いた bvc.lock の tree_hash)(設計書 5節)。
    # tree_hash が None なら、最後に HEAD を変えたとき bvc.lock が無かった(bvc.lock の無いコミット)。
    # 壊れている場合は (True, None) とする(bvc.lock を作らない・黙って書き直さない側に倒す。D-15)。
    # 知らない format なら UnsupportedFormat。
    try:
        data = fsutil.load_json(bvc_dir / LOCKSTATE, LOCKSTATE)
        th = data.get("tree_hash")
        return True, None if th is None else check_sha(th)
    except FileNotFoundError:
        return False, None
    except (CorruptData, UnsafePath) as e:
        logger.warning(
            f"{LOCKSTATE} を読み込めません({e})。次に bvc.lock を書くときに作り直します"
        )
        return True, None


def write_lockstate(bvc_dir: Path, th: str | None) -> None:
    atomic_write_json(
        bvc_dir / LOCKSTATE, {"format": 1, "tree_hash": th}, bvc_dir / "tmp"
    )


# ---------------------------------------------------------------------------
# git の呼び出し
# ---------------------------------------------------------------------------


class Git:
    # 作業フォルダ(cwd)で git を呼ぶ。失敗したら GitFailed。
    # フックから呼ばれた場合は、git が設定した環境変数(GIT_INDEX_FILE など)をそのまま引き継ぐ。

    def __init__(self, workdir: Path):
        self.workdir = workdir

    def run(
        self, *args: str, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        try:
            cp = subprocess.run(
                ["git", *args],
                cwd=str(self.workdir),
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        except OSError as e:
            raise GitFailed(
                f"git を実行できません({e})。git がインストールされ、PATH にあるか確認してください"
            ) from e
        if check and cp.returncode != 0:
            err = cp.stderr.decode("utf-8", "replace").strip()
            raise GitFailed(
                f"git {args[0]} が失敗しました(終了コード {cp.returncode}): {err}"
            )
        return cp

    def _out(self, *args: str) -> str:
        return self.run(*args).stdout.decode("utf-8", "replace").strip()

    def is_work_tree(self) -> bool:
        # 作業フォルダが git の作業ツリーの中にあるか(git が無い場合も False)。
        try:
            cp = self.run("rev-parse", "--is-inside-work-tree", check=False)
        except GitFailed:
            return False
        return cp.returncode == 0 and cp.stdout.strip() == b"true"

    def hooks_dir(self) -> Path:
        # フックのフォルダ(core.hooksPath があればそれ)。
        return self.workdir / self._out("rev-parse", "--git-path", "hooks")

    def head(self) -> str | None:
        # HEAD のコミット(まだコミットが無ければ None)。
        cp = self.run("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        if cp.returncode != 0:
            return None
        return check_git_sha(cp.stdout.decode("ascii", "replace").strip())

    def blob_at(self, rev: str, path: str) -> bytes | None:
        # コミット rev にある path(作業フォルダからの相対パス)の内容。無ければ None。
        out = self.run("ls-tree", "-z", rev, "--", path).stdout
        entries = [e for e in out.split(b"\0") if e]
        if not entries:
            return None
        meta = entries[0].split(b"\t", 1)[0].split()
        if len(meta) != 3 or meta[1] != b"blob":
            return None
        return self.cat_blobs([meta[2].decode("ascii")])[0]

    def staged_blobs(self, path: str) -> list[tuple[int, str]]:
        # ステージ(index)にある path の (stage 番号, blob)。無ければ空。
        out = self.run("ls-files", "-s", "-z", "--", path).stdout
        result = []
        for e in out.split(b"\0"):
            if not e:
                continue
            meta = e.split(b"\t", 1)[0].split()
            if len(meta) == 3:
                result.append((int(meta[2]), meta[1].decode("ascii")))
        return result

    def staged(self, path: str) -> bytes | None:
        # ステージされた path の内容(stage 0)。無ければ None。
        blobs = [b for stage, b in self.staged_blobs(path) if stage == 0]
        return self.cat_blobs(blobs)[0] if blobs else None

    def add(self, path: str) -> None:
        self.run("add", "--", path)

    def history_blobs(self, path: str) -> list[str]:
        # git の全履歴(全ブランチ・タグ・stash・reflog)で path が取った内容(blob)の一覧。
        # path を変更したコミットの差分から、変更前後の blob を集める。マージも各親との差分を見る。
        out = self.run(
            "log",
            "--all",
            "--reflog",
            "--full-history",
            "-m",
            "--no-renames",
            "--format=",
            "--raw",
            "--no-abbrev",
            "-z",
            "--",
            path,
        ).stdout
        blobs: set[str] = set()
        for token in out.split(b"\0"):
            token = token.strip(b"\n")
            if not token.startswith(b":"):
                continue
            meta = token[1:].split()
            if len(meta) < 5:
                raise GitFailed(f"git log の出力を解釈できません: {token[:80]!r}")
            for sha in (meta[2], meta[3]):
                s = sha.decode("ascii", "replace")
                if not _ZERO_SHA_RE.fullmatch(s):
                    blobs.add(check_git_sha(s))
        for _, b in self.staged_blobs(path):
            blobs.add(check_git_sha(b))
        return sorted(blobs)

    def cat_blobs(self, blobs: list[str]) -> list[bytes]:
        # blob の内容をまとめて読む(git cat-file --batch)。読めないものがあれば GitFailed。
        if not blobs:
            return []
        out = self.run(
            "cat-file",
            "--batch",
            input="".join(b + "\n" for b in blobs).encode("ascii"),
        ).stdout
        result = []
        pos = 0
        for b in blobs:
            nl = out.find(b"\n", pos)
            header = out[pos:nl].split() if nl >= 0 else []
            if len(header) != 3 or header[1] != b"blob":
                raise GitFailed(f"git の blob を読めません: {b}")
            size = int(header[2])
            result.append(out[nl + 1 : nl + 1 + size])
            pos = nl + 1 + size + 1
        return result


# ---------------------------------------------------------------------------
# フック(仕様書 3.12節・5.3節)
# ---------------------------------------------------------------------------


def bvc_command() -> str:
    # フックから bvc を起動するコマンド。zipapp から実行中ならその pyz、そうでなければ python -m bvc(I-13)。
    loader = getattr(sys.modules[__name__], "__loader__", None)
    if isinstance(loader, zipimport.zipimporter):
        return f"python {_sh_quote(Path(loader.archive).as_posix())}"
    return "python -m bvc"


def _sh_quote(s: str) -> str:
    # sh のダブルクォートで囲む。
    for ch in ("\\", '"', "$", "`"):
        s = s.replace(ch, "\\" + ch)
    return f'"{s}"'


def hook_line(name: str, workdir: Path, command: str | None = None) -> str:
    # フックで bvc を呼ぶ1行(exec なし)。
    cmd = f"{command or bvc_command()} -C {_sh_quote(workdir.as_posix())} git"
    return {
        "pre-commit": f"{cmd} pre-commit",
        "post-commit": f"{cmd} pin",
        "post-checkout": f'{cmd} post-checkout "$@"',
    }[name]


def hook_script(name: str, workdir: Path, command: str | None = None) -> str:
    return f"#!/bin/sh\n{HOOK_MARK}\nexec {hook_line(name, workdir, command)}\n"


def append_line(name: str, workdir: Path, command: str | None = None) -> str:
    # 既存のフックに追記すべき行。pre-commit は失敗を git に伝える。
    line = hook_line(name, workdir, command)
    return f"{line} || exit $?" if name == "pre-commit" else line


def install_hooks(
    hooks_dir: Path, workdir: Path, command: str | None = None
) -> HooksResult:
    # フックを設置する。無ければ作り、bvc のフックがあれば何もしない。
    # 別の内容のフックは上書きせず、追記すべき行を返す。
    result = HooksResult(changed=False, hooks_dir=str(hooks_dir))
    fsutil.makedirs(hooks_dir)
    for name in HOOK_NAMES:
        path = hooks_dir / name
        try:
            text = fsutil.read_bytes(path).decode("utf-8", "replace")
        except FileNotFoundError:
            text = None
        if text is not None:
            if HOOK_MARK in text or hook_line(name, workdir, command) in text:
                result.already.append(name)
            else:
                result.manual[name] = append_line(name, workdir, command)
            continue
        with open(fsutil.os_path(path), "x", encoding="utf-8", newline="\n") as f:
            f.write(hook_script(name, workdir, command))
        mode = os.stat(fsutil.os_path(path)).st_mode
        os.chmod(
            fsutil.os_path(path), mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        result.installed.append(name)
        result.changed = True
    return result
