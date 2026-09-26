# pytest の共通設定(実装計画書 6.5節)。
# - slow は既定で skip する。--run-slow、またはノード ID(ファイル::クラス::関数)で個別に指定したときは実行する。
# - --force-long-path で、すべてのパスを \\?\ 付きにして実行する(I-16)。

from __future__ import annotations

import os
from pathlib import Path

import pytest

from typing import Final

ENV_RUN_SLOW: Final[str] = "BVC_RUN_SLOW"
ENV_LONG_PATH: Final[str] = "BVC_TEST_LONG_PATH"

_explicit_key = pytest.StashKey[bool]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("bvc")
    group.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help=f"slow のテストも実行する(環境変数 {ENV_RUN_SLOW} も設定する)",
    )
    group.addoption(
        "--force-long-path",
        action="store_true",
        default=False,
        help=f"すべてのパスを \\\\?\\ 付きにして実行する(環境変数 {ENV_LONG_PATH} を設定する)",
    )


def pytest_configure(config: pytest.Config) -> None:
    # 子プロセス(強制終了テスト)にも引き継ぐため、環境変数で渡す。
    # helpers を import する前に設定する必要がある。
    if config.getoption("--run-slow"):
        os.environ[ENV_RUN_SLOW] = "1"
    if config.getoption("--force-long-path"):
        os.environ[ENV_LONG_PATH] = "1"
    # テストが helpers を import しなくても長いパスの設定が効くように、ここで読み込む
    from tests import helpers  # noqa: F401


def _explicit_node_ids(config: pytest.Config) -> list[tuple[Path, str]]:
    # コマンドラインで '::' 付きで指定された項目を (ファイルの絶対パス, '::' 以降) にする。
    result = []
    invocation_dir = Path(config.invocation_params.dir)
    for arg in config.args:
        if "::" not in arg:
            continue
        path_part, rest = arg.split("::", 1)
        result.append(((invocation_dir / path_part).resolve(), rest))
    return result


def _is_explicit(item: pytest.Item, explicit: list[tuple[Path, str]]) -> bool:
    item_path = Path(item.path).resolve()
    item_rest = item.nodeid.split("::", 1)[1] if "::" in item.nodeid else ""
    for path, rest in explicit:
        if path != item_path:
            continue
        # 関数そのもの、パラメータ付きの1件、またはクラス指定の配下
        if item_rest == rest or item_rest.startswith((rest + "::", rest + "[")):
            return True
    return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_slow = config.getoption("--run-slow")
    explicit = _explicit_node_ids(config)
    skip_slow = pytest.mark.skip(reason="時間のかかるテスト(実行するには --run-slow)")
    for item in items:
        # helpers.slow を付けた unittest 形式のテストにも slow マーカーを付ける(-m slow で選べるように)
        if getattr(getattr(item, "obj", None), "_bvc_slow", False):
            item.add_marker(pytest.mark.slow)
        if item.get_closest_marker("slow") is None or run_slow:
            continue
        if _is_explicit(item, explicit):
            item.stash[_explicit_key] = True
        else:
            item.add_marker(skip_slow)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    # 個別に指定された slow の項目は、実行中だけ環境変数を設定する(helpers.slow の判定用)
    if item.stash.get(_explicit_key, False) and not os.environ.get(ENV_RUN_SLOW):
        os.environ[ENV_RUN_SLOW] = "1"
        try:
            return (yield)
        finally:
            del os.environ[ENV_RUN_SLOW]
    return (yield)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # 対象が0件(-m slow で slow が無い場合など)でも成功扱いにする
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
