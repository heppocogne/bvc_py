# tests/conftest.py の slow・--force-long-path の扱い(M0-1、実装計画書 6.5節)。
# 別フォルダに conftest と helpers を複製し、子プロセスの pytest で確認する。

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

SAMPLE = '''
import unittest
import pytest
from tests import helpers

def test_normal():
    pass

@pytest.mark.slow
def test_slow_pytest():
    pass

class TestUnit(unittest.TestCase):
    def test_normal(self):
        pass

    @helpers.slow
    def test_slow_unit(self):
        pass
'''


@pytest.fixture
def sandbox(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    monkeypatch.delenv("BVC_RUN_SLOW", raising=False)
    monkeypatch.delenv("BVC_TEST_LONG_PATH", raising=False)
    tests = pytester.mkpydir("tests")
    for name in ("conftest.py", "helpers.py"):
        (tests / name).write_text((TESTS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tests / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    return pytester


def test_slow_skipped_by_default(sandbox: pytest.Pytester):
    r = sandbox.runpytest_subprocess("tests")
    r.assert_outcomes(passed=2, skipped=2)


def test_run_slow(sandbox: pytest.Pytester):
    r = sandbox.runpytest_subprocess("tests", "--run-slow")
    r.assert_outcomes(passed=4)


def test_run_slow_only(sandbox: pytest.Pytester):
    # helpers.slow を付けた unittest 形式のテストにも slow マーカーが付く
    r = sandbox.runpytest_subprocess("tests", "--run-slow", "-m", "slow")
    r.assert_outcomes(passed=2, deselected=2)


@pytest.mark.parametrize(
    "node",
    [
        "tests/test_sample.py::test_slow_pytest",
        "tests/test_sample.py::TestUnit::test_slow_unit",
    ],
)
def test_explicit_node_id_runs_slow(sandbox: pytest.Pytester, node: str):
    r = sandbox.runpytest_subprocess(node)
    r.assert_outcomes(passed=1)


def test_explicit_class_runs_slow(sandbox: pytest.Pytester):
    r = sandbox.runpytest_subprocess("tests/test_sample.py::TestUnit")
    r.assert_outcomes(passed=2)


def test_explicit_absolute_node_id(sandbox: pytest.Pytester):
    # GUI はファイルを絶対パスで渡すことがある
    path = (sandbox.path / "tests" / "test_sample.py").resolve()
    r = sandbox.runpytest_subprocess(f"{path}::test_slow_pytest")
    r.assert_outcomes(passed=1)


def test_no_tests_is_success(sandbox: pytest.Pytester):
    r = sandbox.runpytest_subprocess("tests", "-m", "no_such_marker")
    assert r.ret == 0


def test_force_long_path_requires_fsutil(sandbox: pytest.Pytester):
    # --force-long-path で fsutil の閾値を差し替える。fsutil が無ければ黙って通常の道筋で流さずに失敗する。
    sandbox.makepyfile(**{"tests/test_lp.py": '''
def test_threshold():
    from bvc import fsutil
    assert fsutil.LONG_PATH_THRESHOLD == 0
'''})
    r = sandbox.runpytest_subprocess("tests/test_lp.py", "--force-long-path")
    try:
        import bvc.fsutil  # noqa: F401
    except ImportError:
        assert r.ret != 0
    else:
        r.assert_outcomes(passed=1)
