# dataclass の定義(全層共通)。設計書 3.1節。
# フィールドは使うマイルストーンで追加する(I-4)。

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChunkRef:
    sha: str      # 圧縮前のデータの SHA-256
    length: int   # 圧縮前のバイト数


@dataclass(frozen=True, slots=True)
class Manifest:
    size: int
    sha256: str                    # ファイル全体の SHA-256
    chunker: dict                  # 記録用(復元には使わない)
    chunks: tuple[ChunkRef, ...]


@dataclass(slots=True)
class PutStats:
    # put_file 1回分の保存の統計。

    size: int = 0          # ファイルのバイト数
    chunks: int = 0        # チャンク数(重複を含む)
    new_chunks: int = 0    # 新しく書き込んだチャンク数
    new_bytes: int = 0     # 新しく書き込んだチャンクの圧縮前のバイト数
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

    stage: str                 # 段階名(例: "put", "write", "verify_chunks")
    done: int                  # 処理済みの量(バイト数または件数)
    total: int | None = None   # 全体の量(不明なら None)
    path: str | None = None    # 処理中のファイル(相対パス)


@dataclass(slots=True)
class StoreVerifyResult:
    # store 単体の全件検証の結果(verify の土台)。

    checked_chunks: int = 0
    checked_manifests: int = 0
    bad_chunks: list[str] = field(default_factory=list)       # 隔離したチャンク
    bad_manifests: list[str] = field(default_factory=list)    # 隔離したマニフェスト
    # 参照先のチャンクが欠損・破損・長さ違いのマニフェスト → その理由
    broken_manifests: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.bad_chunks or self.bad_manifests or self.broken_manifests)


@dataclass(frozen=True, slots=True)
class Head:
    # 現在位置(HEAD)とブランチ。設計書 4.5節。

    at: int      # 現在の版番号
    branch: int  # 現在のブランチ番号


@dataclass(frozen=True, slots=True)
class Commit:
    # 版(不変レコード)。設計書 2.4節。

    id: int                           # 版番号(0 からの単調増加)
    parent: int | None                # 作成時点の親の版番号
    ancestors: tuple[int, ...]        # 親から根までの版番号(祖先トレース用)
    branch: int                       # ブランチ番号
    time: str                         # ISO 8601 形式(タイムゾーン付き)
    kind: str                         # "init", "commit", "auto", "import" のいずれか
    message: str                      # コミットメッセージ
    tree: dict[str, str]              # パス → マニフェスト sha256
    renames: tuple[tuple[str, str, float], ...]  # (消えたパス, 新しいパス, 類似度)
    stats: dict                       # "new_bytes", "total_bytes" など


@dataclass(frozen=True, slots=True)
class Note:
    # コメント(追記のみ)。設計書 2.6節。

    commit_id: int  # コメントを付ける版番号
    time: str       # ISO 8601 形式
    text: str


@dataclass(slots=True)
class WorkState:
    # 作業フォルダの状態(変更検出の結果)。設計書 3.1節。

    tree: dict[str, str]                              # 現状のパス → マニフェスト sha256
    modified: list[str] = field(default_factory=list)  # 内容が変わったパス
    added: list[str] = field(default_factory=list)      # 新しいパス
    renamed: list[tuple[str, str, float]] = field(default_factory=list)  # (消えたパス, 新しいパス, 類似度)
    missing: list[str] = field(default_factory=list)    # 追跡ファイルが無い
    hints: dict[str, list[str]] = field(default_factory=dict)  # missing → パターン外で同一内容のパス
    total_bytes: int = 0   # 追跡ファイルの合計サイズ
    new_bytes: int = 0     # 今回新しく保存したチャンクの圧縮前のバイト数
    fs_time_ns: int = 0    # 走査を始めた時点のファイルシステム上の時刻(index に記録する。I-14)

    @property
    def dirty(self) -> bool:
        # 作業フォルダに未コミットの変更があるか。
        return bool(self.modified or self.added or self.renamed or self.missing)


@dataclass(slots=True)
class LogEntry:
    # log コマンドの出力行。設計書 3.1節。

    id: int                             # 版番号
    commit: Commit | None               # 版ファイルの内容(読めなければ None)
    effective_parent: int | None        # つなぎ直し後の親(None なら根)
    branch_label: str | None = None     # ブランチ名(名前付きブランチの先端なら)
    is_tip: bool = False                # 自分のブランチの先端か
    broken: bool = False                # 壊れた版か(読み込み不可・不正な tree)
    is_current: bool = False            # 現在位置(@)か
    discarded: bool = False             # 削除済みか
    pinned: bool = False                # git に pin されているか
    notes: list[Note] = field(default_factory=list)  # このコミットに付いているコメント


@dataclass(slots=True)
class CommitResult:
    # commit コマンドの結果。設計書 3.1節(I-3)。

    changed: bool              # 変更があったか
    commit: Commit | None      # 作成された版(changed=False なら None)
    state: WorkState           # commit 時点での作業フォルダの状態
    new_branch: bool = False   # 新しいブランチが作られたか


@dataclass(slots=True)
class MoveResult:
    # undo/redo/goto/discard/sync の結果。設計書 3.1節(I-3)。

    changed: bool                      # 現在位置が変わったか
    before: Head                       # 操作前の位置とブランチ
    after: Head                        # 操作後の位置とブランチ
    auto_commit: Commit | None = None  # 自動コミット(あれば)
    restored: list[str] = field(default_factory=list)  # 復元されたパス
    deleted: list[str] = field(default_factory=list)   # 削除されたパス


@dataclass(slots=True)
class RestoreResult:
    # 作業ファイルの復元(worktree.restore)の結果。設計書 3.6節・4.6節。

    written: list[str] = field(default_factory=list)  # 書き出したパス(移動先の内容に置き換えた)
    deleted: list[str] = field(default_factory=list)  # 削除したパス(移動先に無い追跡ファイル)


@dataclass(slots=True)
class Config:
    # 設定(config.json の内容)。設計書 1.1節・3.1節。

    track: list[str]                                    # 追跡対象のパターン
    ignore: list[str] = field(default_factory=list)     # 除外パターン
    rules: list[dict] = field(default_factory=list)     # パターンごとの chunker/compression
    chunker: dict = field(default_factory=lambda: {"name": "fixed", "size": 4194304})  # 既定の分割方式
    compression: str = "auto"                           # 既定の圧縮("auto", "zlib", "none")
    verify_chunks: str = "exists"                       # チャンク検証の強度("exists", "full")
    threads: int = 0                                    # ワーカースレッド数(0 = CPU数)


@dataclass(frozen=True, slots=True)
class BranchInfo:
    # ブランチの情報。設計書 3.1節。

    number: int              # ブランチ番号
    name: str | None         # ブランチ名(無ければ None)
    tip: int                 # 先端の版番号
    parent_tip: int | None   # 親ブランチの先端の版番号(分岐元。無ければ None)


@dataclass(slots=True)
class DiscardResult:
    # discard コマンドの結果。設計書 3.1節。

    changed: bool                      # 削除状態が変わったか
    before: Head                       # 操作前の位置とブランチ(削除前なら @)
    after: Head                        # 操作後の位置とブランチ(削除された場合のみ復元される)
    auto_commit: Commit | None = None  # 自動コミット(現在位置が削除された場合)
    restored: list[str] = field(default_factory=list)  # 復元されたパス(ある場合)
    deleted: list[str] = field(default_factory=list)   # 削除されたパス(ある場合)


@dataclass(slots=True)
class GcReport:
    # gc コマンドの結果。設計書 3.1節。

    deleted_commits: list[int] = field(default_factory=list)  # 削除した版番号
    deleted_manifests: int = 0                                 # 削除したマニフェスト数
    deleted_chunks: int = 0                                    # 削除したチャンク数
    cleaned_tmp: int = 0                                       # 削除した tmp の残骸数
    freed_bytes: int = 0                                       # 解放したバイト数


@dataclass(slots=True)
class VerifyReport:
    # verify コマンドの結果。設計書 3.1節。

    store: StoreVerifyResult = field(default_factory=StoreVerifyResult)
    bad_commits: list[int] = field(default_factory=list)       # 破損した版番号
    repaired_chunks: int = 0                                   # 修復したチャンク数
    repaired_bytes: int = 0                                    # 修復したバイト数
