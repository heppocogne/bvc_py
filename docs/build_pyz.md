# bvc.pyz の作り方

## 概要

`tools/build_pyz.py` は、配布用の1ファイル `bvc.pyz`(Python の zipapp)と、Windows 向けの `bvc.cmd` を作る(実装計画書 M5-4、設計書 1.2節)。
`pyproject.toml` の `version` を `bvc/__init__.py` に埋め込むので、実行時に追加のファイルは要らない。

## 必要な環境

- ビルド: Python 3.10 以上(3.11 以上なら `tomllib`、3.10 では正規表現で `version` を読む)
- 実行: Python 3.10 以上(標準ライブラリだけで動く)

## 使い方

```
python tools/build_pyz.py                       dist/bvc.pyz と dist/bvc.cmd を作る
python tools/build_pyz.py path/to/bvc.pyz       出力先を指定する(bvc.cmd も同じフォルダに作る)
```

## 実行

```
python dist/bvc.pyz --version
python dist/bvc.pyz init --track "*.bin"
python dist/bvc.pyz commit -m "メッセージ"
python dist/bvc.pyz log
```

Windows では、`bvc.pyz` と `bvc.cmd` を同じフォルダに置き、そのフォルダを PATH に加えると `bvc <コマンド>` で実行できる。
`bvc.cmd` の中身は次の3行で、終了コードはそのまま返る(PowerShell の `$LASTEXITCODE`、cmd の `%ERRORLEVEL%`)。

```
@echo off
python "%~dp0bvc.pyz" %*
exit /b %ERRORLEVEL%
```

Linux/macOS では、`chmod +x bvc.pyz` の後に `./bvc.pyz <コマンド>` でも実行できる(先頭行が `#!/usr/bin/env python3`)。

## 仕組み

1. `src/bvc/` を一時フォルダに複製する(`__pycache__` と `.pyc` は除く)。
2. `bvc/__init__.py` を、`__version__ = "<pyproject.toml の version>"` だけのものに置き換える。
3. アーカイブ直下に `__main__.py`(`sys.exit(main())`)を置く。`zipapp` の `main=` 指定で作る入口は戻り値を終了コードにしないため、使わない。
4. `zipapp.create_archive(compressed=True)` で deflate 圧縮の `.pyz` を作り、出力先へ移す。zstd などの新しい圧縮方式は、古い Python で読めないため使わない。
5. 同じフォルダに `bvc.cmd`(改行は CRLF)を書く。

## バージョンを上げるとき

1. `pyproject.toml` の `version` を更新する。
2. `python tools/build_pyz.py` を実行する。

## 確認

`tests/integration/test_pyz.py` が、一時フォルダに作った `bvc.pyz` で基本シナリオと終了コードを確かめる(Windows では `bvc.cmd` も)。
