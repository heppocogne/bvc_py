# dataclass の定義(全層共通)。設計書 3.1節。
# フィールドは使うマイルストーンで追加する(I-4)。

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChunkRef:
    sha: str  # 圧縮前のデータの SHA-256
    length: int  # 圧縮前のバイト数


@dataclass(frozen=True, slots=True)
class Manifest:
    size: int
    sha256: str  # ファイル全体の SHA-256
    chunker: dict  # 記録用(復元には使わない)
    chunks: tuple[ChunkRef, ...]


@dataclass(slots=True)
class PutStats:
    # put_file 1回分の保存の統計。

    size: int = 0  # ファイルのバイト数
    chunks: int = 0  # チャンク数(重複を含む)
    new_chunks: int = 0  # 新しく書き込んだチャンク数
    new_bytes: int = 0  # 新しく書き込んだチャンクの圧縮前のバイト数
    stored_bytes: int = 0  # 新しく書き込んだチャンクファイルのバイト数(ヘッダ込み)

    def add(self, other: PutStats) -> None:
        self.size += other.size
        self.chunks += other.chunks
        self.new_chunks += other.new_chunks
        self.new_bytes += other.new_bytes
        self.stored_bytes += other.stored_bytes


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    # 時間のかかる処理の進捗(GUI・CLI の表示用)。

    stage: str  # 段階名(例: "put", "write", "verify_chunks")
    done: int  # 処理済みの量(バイト数または件数)
    total: int | None = None  # 全体の量(不明なら None)
    path: str | None = None  # 処理中のファイル(相対パス)


@dataclass(slots=True)
class StoreVerifyResult:
    # store 単体の全件検証の結果(verify の土台)。

    checked_chunks: int = 0
    checked_manifests: int = 0
    bad_chunks: list[str] = field(default_factory=list)  # 隔離したチャンク
    bad_manifests: list[str] = field(default_factory=list)  # 隔離したマニフェスト
    # 参照先のチャンクが欠損・破損・長さ違いのマニフェスト → その理由
    broken_manifests: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.bad_chunks or self.bad_manifests or self.broken_manifests)


@dataclass(frozen=True, slots=True)
class Head:
    # 現在位置(HEAD)とブランチ。設計書 4.5節。

    at: int  # 現在の版番号
    branch: int  # 現在のブランチ番号


@dataclass(frozen=True, slots=True)
class Commit:
    # 版(不変レコード)。設計書 2.4節。

    id: int  # 版番号(0 からの単調増加)
    parent: int | None  # 作成時点の親の版番号
    ancestors: tuple[int, ...]  # 親から根までの版番号(祖先トレース用)
    branch: int  # ブランチ番号
    time: str  # ISO 8601 形式(タイムゾーン付き)
    kind: str  # "init", "commit", "auto", "import" のいずれか
    message: str  # コミットメッセージ
    tree: dict[str, str]  # パス → マニフェスト sha256
    renames: tuple[tuple[str, str, float], ...]  # (消えたパス, 新しいパス, 類似度)
    stats: dict  # "new_bytes", "total_bytes" など


@dataclass(frozen=True, slots=True)
class Note:
    # コメント(追記のみ)。設計書 2.6節。

    commit_id: int  # コメントを付ける版番号
    time: str  # ISO 8601 形式
    text: str


@dataclass(slots=True)
class WorkState:
    # 作業フォルダの状態(変更検出の結果)。設計書 3.1節。

    tree: dict[str, str]  # 現状のパス → マニフェスト sha256
    modified: list[str] = field(default_factory=list)  # 内容が変わったパス
    added: list[str] = field(default_factory=list)  # 新しいパス
    renamed: list[tuple[str, str, float]] = field(
        default_factory=list
    )  # (消えたパス, 新しいパス, 類似度)
    missing: list[str] = field(default_factory=list)  # 追跡ファイルが無い
    hints: dict[str, list[str]] = field(
        default_factory=dict
    )  # missing → パターン外で同一内容のパス
    total_bytes: int = 0  # 追跡ファイルの合計サイズ
    new_bytes: int = 0  # 今回新しく保存したチャンクの圧縮前のバイト数
    fs_time_ns: int = (
        0  # 走査を始めた時点のファイルシステム上の時刻(index に記録する。I-14)
    )

    @property
    def dirty(self) -> bool:
        # 作業フォルダに未コミットの変更があるか。
        return bool(self.modified or self.added or self.renamed or self.missing)


@dataclass(slots=True)
class LogEntry:
    # log コマンドの出力行。設計書 3.1節。

    id: int  # 版番号
    commit: Commit | None  # 版ファイルの内容(読めなければ None)
    effective_parent: int | None  # つなぎ直し後の親(None なら根)
    branch_label: str | None = None  # ブランチ名(名前付きブランチの先端なら)
    is_tip: bool = False  # 自分のブランチの先端か
    broken: bool = False  # 壊れた版か(読み込み不可・不正な tree)
    is_current: bool = False  # 現在位置(@)か
    discarded: bool = False  # 削除済みか
    pinned: bool = False  # git に pin されているか
    notes: list[Note] = field(default_factory=list)  # このコミットに付いているコメント


@dataclass(slots=True)
class CommitResult:
    # commit コマンドの結果。設計書 3.1節(I-3)。

    changed: bool  # 変更があったか
    commit: Commit | None  # 作成された版(changed=False なら None)
    state: WorkState  # commit 時点での作業フォルダの状態
    new_branch: bool = False  # 新しいブランチが作られたか


@dataclass(slots=True)
class MoveResult:
    # undo/redo/goto/discard/sync の結果。設計書 3.1節(I-3)。

    changed: bool  # 現在位置が変わったか
    before: Head  # 操作前の位置とブランチ
    after: Head  # 操作後の位置とブランチ
    auto_commit: Commit | None = None  # 自動コミット(あれば)
    restored: list[str] = field(default_factory=list)  # 復元されたパス
    deleted: list[str] = field(default_factory=list)  # 削除されたパス
    skipped: list[int] = field(
        default_factory=list
    )  # --skip-broken で飛ばした壊れた版(近い順)


@dataclass(slots=True)
class SyncResult(MoveResult):
    # sync の結果。仕様書 3.11節。移動しなかった場合も before / after を入れる。

    lock_found: bool = True  # 作業フォルダに bvc.lock があったか
    imported: Commit | None = None  # bvc.lock の内容から作成した版(kind=import)


@dataclass(slots=True)
class RestoreResult:
    # 作業ファイルの復元(worktree.restore)の結果。設計書 3.6節・4.6節。

    written: list[str] = field(
        default_factory=list
    )  # 書き出したパス(移動先の内容に置き換えた)
    deleted: list[str] = field(
        default_factory=list
    )  # 削除したパス(移動先に無い追跡ファイル)


@dataclass(frozen=True, slots=True)
class GitConfig:
    # git 連携の設定(config.json の git)。仕様書 4節。

    enabled: bool = False  # bvc.lock を自動更新する
    lock_file: str = "bvc.lock"  # bvc.lock の場所(作業フォルダからの相対パス)
    pre_commit: str = (
        "snapshot"  # 未コミットの変更があるときの git commit("snapshot", "reject")
    )


@dataclass(slots=True)
class Config:
    # 設定(config.json の内容)。設計書 1.1節・3.1節。

    track: list[str]  # 追跡対象のパターン
    ignore: list[str] = field(default_factory=list)  # 除外パターン
    rules: list[dict] = field(
        default_factory=list
    )  # パターンごとの chunker/compression
    chunker: dict = field(
        default_factory=lambda: {"name": "fixed", "size": 4194304}
    )  # 既定の分割方式
    compression: str = "auto"  # 既定の圧縮("auto", "zlib", "none")
    commit_verify: str = (
        "exists"  # コミット時に再利用する保存データの検査("exists", "full")
    )
    rename_threshold: float = 0.5  # 名前変更とみなす類似度(仕様書 4節)
    threads: int = 0  # ワーカースレッド数(0 = CPU数)
    git: GitConfig = field(default_factory=GitConfig)  # git 連携


@dataclass(frozen=True, slots=True)
class BranchInfo:
    # ブランチの一覧の1行(branch コマンド)。仕様書 3.7節。

    number: int  # 内部のブランチ番号(表示しない。--json での識別用)
    name: str | None  # 名前(無ければ None)
    tip: int | None  # 先端の版番号(有効な版が無ければ None)
    fork: (
        int | None
    )  # 分岐元の版番号(このブランチの最初の版の親。根から始まるなら None)
    is_current: bool  # 現在のブランチ(HEAD.branch)か


@dataclass(slots=True)
class DiscardResult:
    # discard の結果。仕様書 3.8節。

    changed: bool  # 削除印を付けたか(付けられなければ例外にする)
    discarded: int  # 削除印を付けた版番号
    before: Head  # 操作前の位置とブランチ
    after: Head  # 操作後の位置とブランチ(現在位置を消したときは親へ移る)
    auto_commit: Commit | None = None  # 移動の前に作った自動コミット
    restored: list[str] = field(default_factory=list)  # 移動で書き出したパス
    deleted: list[str] = field(default_factory=list)  # 移動で削除したパス


@dataclass(slots=True)
class SquashResult:
    # squash の結果。仕様書 3.14節。

    changed: bool  # 常に True(統合できなければ例外にする)
    commit: Commit  # 統合してできた版(内容は子の版と同じ)
    squashed: list[int]  # 削除印を付けた版番号([親, 子])
    before: Head  # 操作前の位置とブランチ
    after: Head  # 操作後の位置とブランチ(現在位置が子なら統合した版へ移る)


@dataclass(slots=True)
class GcReport:
    # gc の結果。dry_run では「削除する予定のもの」を入れる。仕様書 3.9節、設計書 4.8節。

    changed: bool  # 何か削除したか(dry_run では False)
    dry_run: bool
    deleted_commits: list[int] = field(default_factory=list)  # 版番号
    deleted_manifests: int = 0
    deleted_chunks: int = 0
    deleted_tmp: int = 0  # tmp/ の残骸
    freed_bytes: int = 0  # 削除したファイルの合計サイズ
    skipped: list[str] = field(
        default_factory=list
    )  # 安全のため見送った削除("manifests", "chunks")


@dataclass(slots=True)
class VerifyReport:
    # verify の結果。仕様書 3.10節、設計書 4.9節。
    # 壊れたチャンク・マニフェストは、有効な版(と pin された版)から参照されているものだけを入れる。

    changed: bool  # 隔離・健全性の記録・修復で何かを変えたか
    quick: bool
    repair: bool
    checked_chunks: int = 0
    checked_manifests: int = 0
    checked_commits: int = 0  # 検査した版(有効な版と pin された版)
    bad_chunks: list[str] = field(
        default_factory=list
    )  # 欠損・破損しているチャンク(修復できなかったもの)
    bad_manifests: list[str] = field(
        default_factory=list
    )  # 欠損・破損しているマニフェスト(同上)
    broken_commits: list[int] = field(
        default_factory=list
    )  # 壊れた版(検査の後に残ったもの)
    repaired_chunks: list[str] = field(
        default_factory=list
    )  # --repair で保存し直したチャンク
    repaired_manifests: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.broken_commits


@dataclass(frozen=True, slots=True)
class LockEntry:
    # bvc.lock の files の1項目。仕様書 5.1節。

    size: int
    sha256: str  # ファイル全体の SHA-256
    manifest: str  # マニフェストの sha256(正本)


@dataclass(frozen=True, slots=True)
class LockFile:
    # bvc.lock の内容。仕様書 5.1節。

    bvc_commit: int | None  # 書いたときの版番号(参考情報)
    files: dict[str, LockEntry]  # パス → 内容

    @property
    def tree(self) -> dict[str, str]:
        # 版の tree と同じ形(パス → マニフェスト sha256)。
        return {p: e.manifest for p, e in self.files.items()}


@dataclass(frozen=True, slots=True)
class Pin:
    # git のコミットと bvc の版の対応(pins.jsonl の1行)。設計書 2.6節。

    git: str  # git のコミット
    bvc: int  # 版番号
    tree_hash: str  # 版の tree の tree_hash


@dataclass(slots=True)
class PinResult:
    # git pin の結果。

    changed: bool
    pin: Pin | None = None  # 記録した対応(記録しなければ None)


@dataclass(slots=True)
class HooksResult:
    # git install-hooks の結果。仕様書 3.12節。

    changed: bool
    hooks_dir: str  # フックのフォルダ
    installed: list[str] = field(default_factory=list)  # 設置したフック
    already: list[str] = field(default_factory=list)  # bvc のフックが設置済み
    # LEGACY-HOOK-C(削除予定): 絶対パスの -C を相対パスに直したフック(already にも含む)。--json の出力項目でもある
    updated: list[str] = field(default_factory=list)
    manual: dict[str, str] = field(default_factory=dict)  # 既存のフック → 追記すべき行


@dataclass(slots=True)
class PreCommitResult:
    # git pre-commit の結果。仕様書 5.3節。

    changed: bool
    auto_commit: Commit | None = None  # snapshot で作った自動コミット
    staged_ok: bool = (
        False  # ステージされた bvc.lock を検査して通ったか(無ければ False)
    )


@dataclass(slots=True)
class PostCheckoutResult:
    # git post-checkout の結果。仕様書 5.3節。

    changed: bool
    synced: bool = False  # sync を実行したか
    sync: SyncResult | None = None


@dataclass(slots=True)
class PresetInfo:
    # init --preset で使えるプリセット1つ(preset list / show)。仕様書 3.1.1節・3.1.2節。

    name: str
    description: str
    source: str  # "builtin"(組み込み)/ "user"(ユーザー定義)
    overrides: bool = False  # ユーザー定義が同名の組み込みを上書きしているか
    include: list[str] = field(default_factory=list)  # 取り込むプリセット(書かれた通り)
    track: list[str] = field(default_factory=list)  # include を展開した追跡パターン
    ignore: list[str] = field(default_factory=list)  # include を展開した除外パターン


@dataclass(slots=True)
class PresetList:
    # 使えるプリセットの一覧と、ユーザー定義のファイルの場所。

    file: str  # ユーザー定義プリセットのファイルのパス
    file_exists: bool
    presets: list[PresetInfo] = field(default_factory=list)
