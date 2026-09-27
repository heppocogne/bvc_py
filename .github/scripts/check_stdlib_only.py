# 実行時依存が標準ライブラリのみ(dependencies = [])であることを確かめる。
# 使い方: python .github/scripts/check_stdlib_only.py
# 依存があれば SystemExit(1) で失敗させる。

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    text = (PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
    if m is None:
        print("エラー: dependencies が見つかりません", file=sys.stderr)
        return 1
    if m.group(1).strip() != "":
        print(
            "エラー: 実行時依存は標準ライブラリのみ(dependencies = [])を維持すること",
            file=sys.stderr,
        )
        return 1
    print("dependencies = [] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
