# 例外の定義(全層共通)。設計書 3.7節。
# exit_code は仕様書 2.2節の終了コード。細分したクラスは親の値を引き継ぐ(I-5)。

from __future__ import annotations

from typing import Any


class BvcError(Exception):
    # bvc のエラーの基底。終了コード 1。

    exit_code = 1

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message)
        self.message = message
        # 表示・--json 用の付加情報(パス、ハッシュなど)
        self.details = details


class UsageError(BvcError):
    # 引数の誤り。
    exit_code = 2


class SafetyAbort(BvcError):
    # 安全のため中止した。
    exit_code = 3


class MissingFiles(SafetyAbort):
    # 追跡ファイルが欠落している(--allow-missing の指定なし)。
    pass


class FileBusy(SafetyAbort):
    # ファイルを開けない・置き換えられない(他のアプリが使用中など)。
    pass


class FileChanging(SafetyAbort):
    # 読み取りの前後でファイルが変わった(書き込み中)。
    pass


class Locked(SafetyAbort):
    # 別の bvc がリポジトリを使用中(ロックファイルがある)。
    pass


class PinnedCommit(SafetyAbort):
    # git から参照されている版を消そうとした。
    pass


class DiskFull(SafetyAbort):
    # 空き容量が足りない。
    pass


class CannotMove(BvcError):
    # 要求された移動ができない(根での undo、先端での redo)。データは変わっていない。
    exit_code = 4


class IntegrityError(BvcError):
    # 記録されたデータの不整合。
    pass


class CorruptData(IntegrityError):
    # ハッシュの不一致・欠損・形式エラー。
    pass


class BrokenVersion(IntegrityError):
    # 壊れた版への移動。
    pass


class UnsafePath(IntegrityError):
    # 記録されたパス・ハッシュ・番号が不正で、パスとして使えない。
    pass


class UnsupportedFormat(BvcError):
    # 知らない format 番号・codec ID。何も書き込まずに中止する。
    pass


class RevisionError(BvcError):
    # リビジョン式を解決できない。
    pass
