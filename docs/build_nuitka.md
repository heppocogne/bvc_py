# bvc.exe の作り方(Nuitka)

## 概要

`tools/build_nuitka.py` は、[Nuitka](https://nuitka.net/) で1ファイルの実行ファイル(Windows なら `bvc.exe`、それ以外は `bvc`)を作る。
`tools/build_pyz.py`(zipapp)と同じく、`pyproject.toml` の `version` を `bvc/__init__.py` に埋め込んでから組み立てる(コンパイル後は `pyproject.toml` を読めないため)。

zipapp(`dist/bvc.pyz`)と違い、実行環境に Python が要らない1個の実行ファイルになる。その代わりコンパイルに C コンパイラが要り、ビルドに数分かかる。

## 必要な環境

- ビルド: Python 3.11 以上、`pip install -e ".[build]"` で Nuitka を入れる。加えて C コンパイラが要る(Windows は Visual Studio Build Tools、Linux/macOS は gcc/clang)。
- 実行: 追加のインストールは不要(Python も不要)。

## 使い方

```
python tools/build_nuitka.py                     dist/bvc.exe を作る(Windows。他 OS では拡張子なし)
python tools/build_nuitka.py path/to/bvc.exe     出力先を指定する
```

## 実行

```
dist\bvc.exe --version
dist\bvc.exe init --track "*.bin"
dist\bvc.exe commit -m "メッセージ"
dist\bvc.exe log
```

## 仕組み

1. `tools/build_pyz.py` と同じく、一時フォルダに `bvc/`(`src/bvc/` の複製、`__pycache__`・`.pyc`・`bvc/__main__.py` を除く)と、`from bvc.main import main; sys.exit(main())` だけの `__main__.py` を作る(`bvc/__main__.py` は `python -m bvc` 専用のため使わない。I-13)。
2. `bvc/__init__.py` を `__version__ = "<pyproject.toml の version>"` だけのものに置き換える。
3. `python -m nuitka --onefile` でその `__main__.py` を実行ファイルにする。
4. 出来た実行ファイルを出力先へ移す(途中で失敗しても前の実行ファイルを壊さない)。

### `--no-deployment-flag=self-execution` について

Nuitka の onefile 実行ファイルには、自分自身を `-m`/`-c` 付きで再帰的に呼び出していないか(multiprocessing の無限増殖など)を検知する保護が入っている。
これが `bvc commit -m "..."` の `-m`(コミットメッセージの短縮オプション)を誤検知し、`Error, the program tried to call itself with '-m' argument` で止まる。
`bvc` は他プロセスを `-m`/`-c` で起動しないため、ビルド時にこの保護だけを外している。

## バージョンを上げるとき

1. `pyproject.toml` の `version` を更新する。
2. `python tools/build_nuitka.py` を実行する。

## 確認

`tests/integration/test_nuitka.py`(`slow` マーカー。`--run-slow` で実行)が、一時フォルダに作った実行ファイルで基本シナリオ(`commit -m` を含む)と終了コードを確かめる。
Nuitka が入っていない環境では自動で skip する。
