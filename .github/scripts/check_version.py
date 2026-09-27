# タグ / pyproject.toml / bvc.pyz の版数が一致することを確かめる。
# 使い方: python .github/scripts/check_version.py <タグ名(v 接頭辞の有無は問わない)>
# 不一致があれば SystemExit(1) で失敗させる。

from __future__ import annotations

import re
import sys
from pathlib import Path
from subprocess import check_output

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent


def read_pyproject_version(pyproject: Path = PROJECT_DIR / "pyproject.toml") -> str:
    text = pyproject.read_text(encoding="utf-8")
    m = re.search(r'(?ms)^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"', text)
    if m is None:
        raise ValueError(f"version が見つかりません: {pyproject}")
    return m.group(1)


def read_pyz_version(pyz: Path = PROJECT_DIR / "dist" / "bvc.pyz") -> str:
    out = check_output([sys.executable, str(pyz), "--version"], text=True)
    return out.strip().split()[-1]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(f"使い方: python {Path(__file__).name} <タグ名>", file=sys.stderr)
        return 2
    tag = argv[0].lstrip("v")
    try:
        pyproject_version = read_pyproject_version()
        pyz_version = read_pyz_version()
    except (OSError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"tag={tag} pyproject={pyproject_version} pyz={pyz_version}")
    if tag != pyproject_version:
        print(
            f"エラー: タグと pyproject.toml が不一致: {tag} != {pyproject_version}",
            file=sys.stderr,
        )
        return 1
    if pyz_version != pyproject_version:
        print(
            f"エラー: pyz と pyproject.toml が不一致: {pyz_version} != {pyproject_version}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
