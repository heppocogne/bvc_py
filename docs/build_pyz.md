# .pyz ファイル生成ガイド

## 概要

`scripts/build_pyz.py` は、スタンドアロンの Python Zip Archive (`.pyz`) ファイルを生成します。
`pyproject.toml` からバージョンを自動抽出してハードコードするため、追加の依存管理が不要です。

## 必要な環境

- **Python 3.11 以上** (build スクリプトが `tomllib` を使用)

## 使用方法

### デフォルト出力先 (`dist/bvc.pyz`)

```bash
python scripts/build_pyz.py
```

### カスタム出力先

```bash
python scripts/build_pyz.py path/to/output.pyz
```

## 生成される .pyz の実行

```bash
# バージョン表示
python dist/bvc.pyz --version

# ヘルプ表示
python dist/bvc.pyz --help

# コマンド実行
python dist/bvc.pyz init
python dist/bvc.pyz commit -m "メッセージ"
python dist/bvc.pyz log
```

## .pyz ファイルの配布

生成された `.pyz` ファイルは：
- ✅ スタンドアロンで実行可能（他の依存不要）
- ✅ `pip install` の必要がない
- ✅ バージョン情報が組み込まれている
- ✅ 複数の OS で実行可能

## バージョン更新時の手順

1. `pyproject.toml` の `version` を更新
2. `python scripts/build_pyz.py` を実行
3. 新しい `.pyz` ファイルが生成される（バージョンは自動的に更新）

## 仕組み

```
┌─────────────────────────────────┐
│ pyproject.toml                  │
│   version = "0.1.0"             │
└────────────┬────────────────────┘
             │ (tomllib で読込)
             ▼
┌─────────────────────────────────┐
│ build_pyz.py                    │
│  ├─ バージョン抽出              │
│  ├─ __init__.py にハードコード  │
│  └─ .pyz 生成                   │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ dist/bvc.pyz                    │
│  └─ bvc/__init__.py             │
│      __version__ = "0.1.0" ◄─── │ ハードコード
└─────────────────────────────────┘
```

## トラブルシューティング

### Python 3.10 以前で実行

```
エラー: Python 3.11 以上が必要です
```

**解決方法**: Python 3.11 以上でビルドしてください。

### `pyproject.toml` が見つからない

ビルドスクリプトは `scripts/build_pyz.py` から相対的に `pyproject.toml` を探しています。
プロジェクトルートから実行してください。

```bash
cd /path/to/bvc-py
python scripts/build_pyz.py
```
