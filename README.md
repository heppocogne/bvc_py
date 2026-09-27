# bvc_py
![](https://github.com/heppocogne/bvc_py/actions/workflows/ci_py.yaml/badge.svg)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Gitleaks](https://img.shields.io/badge/protected%20by-gitleaks-blue)](https://github.com/gitleaks/gitleaks-action)

![Claude](https://img.shields.io/badge/claude-%23D97757.svg?style=for-the-badge&logo=claude&logoColor=white)
![Python](https://img.shields.io/badge/python-%233670A0.svg?style=for-the-badge&logo=python&logoColor=ffdd54)
![GitHub Actions](https://img.shields.io/badge/github%20actions-%232671E5.svg?style=for-the-badge&logo=githubactions&logoColor=white)

大容量バイナリファイル向けの**ローカル専用**バージョン管理ツールです(Python版)。
[Jujutsu](https://github.com/jj-vcs/jj)を参考に、ステージングなし・自動コミットありの「undo/redoの発展形」という操作感を目指しています。
M1〜M7のマイルストーンに沿って開発中のα版で、現在の進捗は[実装計画書](docs/05_実装計画書.md)を参照して下さい。

## 実行要件
| 機能 | Python | 依存関係 |
|---|---|---|
| 実行のみ | 3.11以上 | 無し(標準ライブラリのみ) |
| 開発・テスト | 3.11以上 | pytest>=9.0.0, pytest-cov>=7.0.0 |

## インストール
実行のみであれば、標準ライブラリだけで動きます。
```bash
git clone https://github.com/heppocogne/bvc_py.git
cd bvc_py
pip install .
```
開発する場合は、pytestなどの開発用依存も含めてインストールしてください。
```bash
pip install -e ".[dev]"
```

## 使い方
### 基本操作
ファイルのあるディレクトリで`bvc init`を実行するとリポジトリができます。
以降は`bvc commit`で現状を記録し、`bvc log`で履歴を確認します。

```bash
cd my-project
bvc init
echo hello > data.bin
bvc commit -m "初回コミット"
bvc log
```

戻したいときは`bvc undo`、その取り消しは`bvc redo`、特定の版に直接移動したいときは`bvc goto <版>`を使います。

### その他コマンド
```bash
usage: bvc [-C <パス>] [--json] [-q] [--help] [--version] <コマンド> ...

options:
  -C <パス>    作業フォルダを指定する(省略時はカレントから上位へ .bvc を探す)
  --json     結果を JSON で出力する
  -q         通常の出力を抑制する
  --help     ヘルプを表示
  --version  バージョンを表示

コマンド:
  <コマンド>
    init     リポジトリを作成する
    commit   追跡ファイルの現状を版として記録する
    log      版のツリーを表示する
    undo     1つ前の版に戻る
    redo     戻したのを取り消す(先端へ進む)
    goto     指定の版へ移動する
    note     版にコメントを追記する
    branch   ブランチの一覧・名前の付け外し
    discard  版に削除の印を付ける(データは gc まで残る)
    gc       削除済みの版と不要なデータを消す
    verify   保存データを検査する(異常が残れば終了コード 1)
    sync     bvc.lock の内容に作業ファイルを合わせる(git 連携)
    git      git 連携(フックの設置、フック用のコマンド)
```
`bvc <command> --help`で各コマンドのオプションなどが確認できます。

## ドキュメント
仕様や内部設計は`docs/`にまとめてあります。

| 文書 | 内容 |
|---|---|
| [01_要件定義書](docs/01_要件定義書.md) | 目的・制約・機能/非機能要件 |
| [02_仕様書](docs/02_仕様書.md) | コマンド・終了コード・設定ファイルなどの外部仕様 |
| [03_設計書](docs/03_設計書.md) | モジュール構成・データ構造などの内部設計 |
| [04_テスト観点](docs/04_テスト観点.md) | テスト観点の一覧 |
| [05_実装計画書](docs/05_実装計画書.md) | マイルストーンごとの作業項目と進捗 |
| [06_実機確認手順書](docs/06_実機確認手順書.md) | 実際の環境での動作確認方法 |

## 開発
```bash
pip install -e ".[dev]"
python -m pytest
```
カバレッジを見たいときは`python -m pytest --cov=bvc --cov-report=term-missing`を使ってください。

## 謝辞
[Jujutsu](https://github.com/jj-vcs/jj)の操作感に着想を得ています。
