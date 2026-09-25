# 原子的書き込み・JSONL の追記/読み込み・ロック・パスの検査・glob 照合。設計書 4.7節・4.10節。
#
# パスの持ち方(設計書 1.1節): 上位の層は Path(または基準フォルダ + '/' 区切りの相対パス)で扱い、
# OS の API に渡す直前に os_path() で実際の文字列にする。変換はこのモジュールの中だけで行う。

from __future__ import annotations

import datetime
import functools
import json
import os
import re
import socket
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Final, Iterable

from bvc.errors import CorruptData, Locked, UnsafePath, UnsupportedFormat

IS_WINDOWS: Final[bool] = os.name == "nt"

# この長さ以上の絶対パスを \\?\ 付きにする(フォルダ作成の上限が 248 のため)。
# テストでは 0 に差し替えて、常に \\?\ 付きの経路を通す(I-16)。
# 関数の中では、呼び出しのたびにモジュール変数として参照すること。
LONG_PATH_THRESHOLD: Final[int] = 248

# 障害注入のフック(I-11)。段階名を渡して呼ぶ。既定は None。
_fault_hook: Callable[[str], None] | None = None

# 現在の形式番号
FORMAT: Final[int] = 1


def fault(stage: str) -> None:
    # 障害注入の地点。テストが _fault_hook を差し込んだときだけ、それを呼ぶ。
    hook = _fault_hook
    if hook is not None:
        hook(stage)


def now_iso() -> str:
    # 記録用の現在時刻(ローカル時刻、秒まで、UTC オフセット付き)。
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 長いパス(I-16)
# ---------------------------------------------------------------------------

_PREFIX: Final[str] = "\\\\?\\"
_UNC_PREFIX: Final[str] = "\\\\?\\UNC\\"


def os_path(base: str | os.PathLike[str], rel: str = "") -> str:
    # base/rel を OS の API に渡す文字列にする。
    # Windows で LONG_PATH_THRESHOLD 以上の長さなら \\?\ を付ける(UNC は \\?\UNC\)。
    # 他の OS、短いパスではそのまま返す。rel は '/' 区切りの相対パス。
    p = os.fspath(base)
    if rel:
        p = os.path.join(p, *rel.split("/"))
    if not IS_WINDOWS or p.startswith(_PREFIX):
        return p
    if len(p) < LONG_PATH_THRESHOLD:
        return p
    p = os.path.abspath(p)  # \\?\ 付きでは '/' や '..' が解釈されないため、先に正規化する
    if p.startswith("\\\\"):
        return _UNC_PREFIX + p[2:]
    return _PREFIX + p


def _strip_prefix(p: str) -> str:
    if p.startswith(_UNC_PREFIX):
        return "\\\\" + p[len(_UNC_PREFIX):]
    if p.startswith(_PREFIX):
        return p[len(_PREFIX):]
    return p


def _real(p: str | os.PathLike[str]) -> str:
    r = _strip_prefix(os.path.realpath(os_path(p)))
    return os.path.normcase(r) if IS_WINDOWS else r


def same_or_inside(root: str | os.PathLike[str], p: str | os.PathLike[str]) -> bool:
    # p が root と同じか、その内側にあるか。
    # 両側を realpath してから比べる(ネットワークドライブ・subst が UNC に書き換わるため)。
    # Windows では大文字小文字を区別しない。
    r = _real(root)
    q = _real(p)
    if q == r:
        return True
    return q.startswith(r.rstrip(os.sep) + os.sep)


def is_link_or_reparse(st: os.stat_result) -> bool:
    # シンボリックリンク、またはリパースポイント(ジャンクションなど)か。lstat の結果を渡す。
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


# ---------------------------------------------------------------------------
# 記録された値の検査(仕様書 2.9節、設計書 4.10節、I-6)
# ---------------------------------------------------------------------------

_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ID_STR_RE: Final[re.Pattern[str]] = re.compile(r"0|[1-9][0-9]{0,15}")
MAX_ID: Final[int] = 10**15
_INVALID_CHARS: Final[frozenset[str]] = frozenset('<>:"|?*\\')
_RESERVED: Final[frozenset[str]] = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{c}" for c in "0123456789¹²³"]
    + [f"LPT{c}" for c in "0123456789¹²³"]
)


def check_sha(s: Any) -> str:
    # 小文字16進64桁のハッシュ値か検査する。不正なら UnsafePath。
    if type(s) is not str or not _SHA_RE.fullmatch(s):
        raise UnsafePath(f"ハッシュ値の形式が不正です: {s!r:.80}")
    return s


def check_id(n: Any) -> int:
    # 版番号・ブランチ番号(0 以上の整数)か検査する。不正なら UnsafePath。
    if type(n) is not int or not 0 <= n <= MAX_ID:
        raise UnsafePath(f"番号の形式が不正です: {n!r:.80}")
    return n


def check_id_str(s: Any) -> int:
    # ファイル名などの文字列の番号(先頭ゼロなし)を検査して int にする。
    if type(s) is not str or not _ID_STR_RE.fullmatch(s):
        raise UnsafePath(f"番号の形式が不正です: {s!r:.80}")
    return check_id(int(s))


def _check_element(e: str, p: str) -> None:
    if e in ("", ".", ".."):
        raise UnsafePath(f"パスに空・'.'・'..' の要素があります: {p!r}", path=p)
    if e[-1] in ". ":
        raise UnsafePath(f"末尾がドットまたは空白の要素があります: {p!r}", path=p)
    for c in e:
        if c in _INVALID_CHARS or ord(c) < 32 or ord(c) == 127:
            raise UnsafePath(f"使用できない文字を含みます: {p!r}", path=p)
    if e.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        raise UnsafePath(f"Windows の予約名を含みます: {p!r}", path=p)


def check_relpath(p: Any) -> str:
    # 記録された相対パスを検査し、NFC に正規化して返す。不正なら UnsafePath。
    # '/' 区切りで、絶対パス・ドライブ指定・UNC・'\\'・'.'・'..'・空の要素・先頭の '.bvc'・
    # 予約名・末尾のドットと空白・使用できない文字を含まないこと(仕様書 2.9節)。
    # OS によらず同じ規則で検査する。
    if type(p) is not str or not p:
        raise UnsafePath(f"パスが空か文字列ではありません: {p!r:.80}")
    p = unicodedata.normalize("NFC", p)
    if p.startswith("/"):
        raise UnsafePath(f"絶対パスは使えません: {p!r}", path=p)
    elements = p.split("/")
    for e in elements:
        _check_element(e, p)
    if elements[0].casefold() == ".bvc":
        raise UnsafePath(f".bvc の中は指定できません: {p!r}", path=p)
    return p


def resolve_in_workdir(workdir: str | os.PathLike[str], rel: str) -> Path:
    # 作業ファイルへ書き込む・削除する直前の検査。workdir/rel を返す。
    # rel を check_relpath で検査し、途中のフォルダと対象自身がシンボリックリンク・
    # ジャンクションでないこと、途中がフォルダであること、結果が workdir の内側に
    # あることを確認する。途中のフォルダが存在しなければ、そこから先の確認は省く(後で作るため)。
    rel = check_relpath(rel)
    workdir = Path(workdir)
    current = workdir
    parts = rel.split("/")
    for i, part in enumerate(parts):
        current = current / part
        try:
            st = os.lstat(os_path(current))
        except FileNotFoundError:
            break
        if is_link_or_reparse(st):
            raise UnsafePath(f"パスの途中にリンクがあります: {rel!r}", path=rel)
        if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"パスの途中がフォルダではありません: {rel!r}", path=rel)
    target = workdir.joinpath(*parts)
    if not same_or_inside(workdir, target) or _real(target) == _real(workdir):
        raise UnsafePath(f"作業フォルダの外を指しています: {rel!r}", path=rel)
    return target


# ---------------------------------------------------------------------------
# 原子的書き込み・JSON(設計書 2.1節・4.7節)
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> bytes:
    # 正規化 JSON(キー順固定、区切りの空白なし、UTF-8)。マニフェスト名などのハッシュに使う。
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def fsync_dir(path: str | os.PathLike[str]) -> None:
    # フォルダのエントリの変更を確定させる(POSIX のみ。Windows では何もしない)。
    if IS_WINDOWS:
        return
    fd = os.open(os_path(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def makedirs(path: str | os.PathLike[str]) -> None:
    os.makedirs(os_path(path), exist_ok=True)


def new_tmp_path(tmpdir: str | os.PathLike[str]) -> Path:
    # tmpdir の中に一意な一時ファイル名を用意する(tmpdir が無ければ作る)。
    makedirs(tmpdir)
    return Path(tmpdir) / f"{uuid.uuid4().hex}.tmp"


def remove_quietly(path: str | os.PathLike[str]) -> None:
    try:
        os.remove(os_path(path))
    except FileNotFoundError:
        pass


def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str], stage: str) -> None:
    # os.replace の前に障害注入の地点 'replace:<stage>' を置いたもの。
    fault(f"replace:{stage}")
    os.replace(os_path(src), os_path(dst))


def atomic_write(
    path: str | os.PathLike[str], data: bytes, tmpdir: str | os.PathLike[str]
) -> None:
    # tmp に書いて fsync し、os.replace で置き換える。途中で失敗しても元のファイルは残る。
    # tmpdir は置き換え先と同じボリュームにあること(通常は .bvc/tmp)。
    # 障害注入の地点は、tmp を書き終えて置き換える直前('atomic_write:<ファイル名>')。
    path = Path(path)
    tmp = new_tmp_path(tmpdir)
    try:
        with open(os_path(tmp), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        fault(f"atomic_write:{path.name}")
        makedirs(path.parent)
        os.replace(os_path(tmp), os_path(path))
    except BaseException:
        remove_quietly(tmp)
        raise
    fsync_dir(path.parent)


def atomic_write_json(
    path: str | os.PathLike[str], obj: Any, tmpdir: str | os.PathLike[str]
) -> None:
    """正規化 JSON + 末尾の改行で原子的に書く。"""
    atomic_write(path, canonical_json(obj) + b"\n", tmpdir)


def read_bytes(path: str | os.PathLike[str]) -> bytes:
    with open(os_path(path), "rb") as f:
        return f.read()


def check_format(obj: Any, what: str, known: Iterable[int] = (FORMAT,)) -> dict:
    # JSON の値が dict で、既知の format を持つか検査する。
    # format が無い・整数でないなら CorruptData、知らない番号なら UnsupportedFormat(V-2)。
    if not isinstance(obj, dict):
        raise CorruptData(f"{what}: 形式が不正です")
    fmt = obj.get("format")
    if type(fmt) is not int:
        raise CorruptData(f"{what}: format がありません")
    if fmt not in tuple(known):
        raise UnsupportedFormat(
            f"{what}: 対応していない形式です(format={fmt})。新しい版の bvc で作られた可能性があります"
        )
    return obj


def load_json(path: str | os.PathLike[str], what: str) -> dict:
    # JSON ファイルを読み、format を検査して返す。
    # ファイルが無ければ FileNotFoundError。解析できない・形式不正なら CorruptData。
    # 読み込み自体の OSError(使用中など)は破損の証拠ではないので、そのまま送出する(D-15)。
    data = read_bytes(path)
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise CorruptData(f"{what}: JSON として読めません") from e
    return check_format(obj, what)


# ---------------------------------------------------------------------------
# JSONL(追記のみ)
# ---------------------------------------------------------------------------

def append_jsonl(path: str | os.PathLike[str], obj: dict) -> None:
    # 1行を末尾に追記して fsync する。書き換え・切り詰めはしない。
    # 前回の書き込みが途中で終わって最終行に改行が無い場合は、先に改行を足して、
    # 新しい行が壊れた行とつながらないようにする。
    path = Path(path)
    line = canonical_json(obj) + b"\n"
    makedirs(path.parent)
    fault(f"append_jsonl:{path.name}")
    with open(os_path(path), "ab+") as f:
        end = f.seek(0, os.SEEK_END)
        if end > 0:
            f.seek(end - 1)
            if f.read(1) != b"\n":
                line = b"\n" + line
            f.seek(0, os.SEEK_END)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(
    path: str | os.PathLike[str], what: str, known: Iterable[int] = (FORMAT,)
) -> tuple[list[dict], list[str]]:
    # JSONL を読み、(記録の一覧, 警告の一覧) を返す。ファイルが無ければ空。
    # - 最終行(改行で終わっていない行)が壊れていれば、書き込み途中とみなして黙って無視する。
    # - 途中の行が壊れていれば、読み飛ばして警告を返す(C-8)。
    # - 知らない format の行は、壊れた行と区別して UnsupportedFormat で中止する(設計書 2.6節)。
    known = tuple(known)
    try:
        data = read_bytes(path)
    except FileNotFoundError:
        return [], []
    records: list[dict] = []
    warnings: list[str] = []
    lines = data.split(b"\n")
    last = len(lines) - 1  # data が改行で終わっていれば lines[last] は空
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            obj = check_format(json.loads(raw.decode("utf-8")), what, known)
        except UnsupportedFormat:
            raise
        except (UnicodeDecodeError, ValueError, CorruptData):
            if i != last:
                warnings.append(f"{what}: {i + 1}行目が壊れているため読み飛ばしました")
            continue
        records.append(obj)
    return records, warnings


# ---------------------------------------------------------------------------
# ロック(設計書 4.7節)
# ---------------------------------------------------------------------------

class FileLock:
    """O_CREAT|O_EXCL で作るロックファイル。with 文で使える。

    既にあれば Locked を送出する(内容を details["info"] に入れる)。古いロックかどうかは判定しない。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        if self._held:
            raise RuntimeError("ロックは取得済みです")
        info = {
            "format": FORMAT,
            "pid": os.getpid(),
            "time": now_iso(),
            "host": socket.gethostname(),
        }
        try:
            fd = os.open(os_path(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, PermissionError) as e:
            # Windows では削除待ちのファイルに対して PermissionError になる。どちらも使用中として止める
            other = self.read_info()
            raise Locked(
                "別の bvc がこのリポジトリを使用中です"
                f"(ロックファイル: {self.path}"
                + (f"、pid={other.get('pid')}、host={other.get('host')}、time={other.get('time')}" if other else "")
                + ")。使用中の bvc が無いことを確かめてから、ロックファイルを削除してください",
                path=str(self.path),
                info=other,
            ) from e
        try:
            os.write(fd, canonical_json(info) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._held = True

    def read_info(self) -> dict:
        # ロックファイルの内容(読めなければ空)。
        try:
            obj = json.loads(read_bytes(self.path).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return {}
        return obj if isinstance(obj, dict) else {}

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        remove_quietly(self.path)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# glob 照合(仕様書 2.4節)
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> str:
    # '/' 区切りの glob を正規表現にする。
    # '*' と '?' は '/' を越えない。'**' だけの要素は0階層以上のフォルダに一致する。
    # それ以外の文字はそのまま照合する。
    elements = pattern.split("/")
    out: list[str] = []
    n = len(elements)
    for i, e in enumerate(elements):
        last = i == n - 1
        if e == "**":
            # 末尾の '**' は残り全部('a/**' は a の下のすべて)、途中の '**' は0階層以上のフォルダ
            out.append(".*" if last else "(?:[^/]*/)*")
            continue
        for c in e:
            if c == "*":
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            else:
                out.append(re.escape(c))
        if not last:
            out.append("/")
    return "".join(out)


@functools.lru_cache(maxsize=1024)
def compile_glob(pattern: str, ignore_case: bool = IS_WINDOWS) -> re.Pattern[str]:
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(_glob_to_regex(pattern), flags | re.DOTALL)


def glob_match(pattern: str, relpath: str, ignore_case: bool = IS_WINDOWS) -> bool:
    # '/' 区切りの相対パスが glob に一致するか。Windows では既定で大文字小文字を区別しない。
    return compile_glob(pattern, ignore_case).fullmatch(relpath) is not None
