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
    """put_file 1回分の保存の統計。"""

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
    """時間のかかる処理の進捗(GUI・CLI の表示用)。"""

    stage: str                 # 段階名(例: "put", "write", "verify_chunks")
    done: int                  # 処理済みの量(バイト数または件数)
    total: int | None = None   # 全体の量(不明なら None)
    path: str | None = None    # 処理中のファイル(相対パス)


@dataclass(slots=True)
class StoreVerifyResult:
    """store 単体の全件検証の結果(verify の土台)。"""

    checked_chunks: int = 0
    checked_manifests: int = 0
    bad_chunks: list[str] = field(default_factory=list)       # 隔離したチャンク
    bad_manifests: list[str] = field(default_factory=list)    # 隔離したマニフェスト
    # 参照先のチャンクが欠損・破損・長さ違いのマニフェスト → その理由
    broken_manifests: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.bad_chunks or self.bad_manifests or self.broken_manifests)
