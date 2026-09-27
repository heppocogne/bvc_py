# タグ / pyproject.toml / 配布物(bvc.pyz、bvc.exe など)の版数が一致することを確かめる。
# 使い方: python .github/scripts/check_version.py <タグ名(v 接頭辞の有無は問わない)> <配布物のパス>...
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


def read_artifact_version(artifact: Path) -> str:
    # .pyz は python 経由、それ以外(実行ファイル)は直接起動する
    cmd = (
        [sys.executable, str(artifact)]
        if artifact.suffix == ".pyz"
        else [str(artifact)]
    )
    out = check_output([*cmd, "--version"], text=True)
    return out.strip().split()[-1]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            f"使い方: python {Path(__file__).name} <タグ名> <配布物のパス>...",
            file=sys.stderr,
        )
        return 2
    tag = argv[0].lstrip("v")
    artifacts = [Path(p) for p in argv[1:]]
    try:
        pyproject_version = read_pyproject_version()
        artifact_versions = {a: read_artifact_version(a) for a in artifacts}
    except (OSError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    print(f"tag={tag} pyproject={pyproject_version}")
    ok = tag == pyproject_version
    if not ok:
        print(
            f"エラー: タグと pyproject.toml が不一致: {tag} != {pyproject_version}",
            file=sys.stderr,
        )
    for artifact, version in artifact_versions.items():
        print(f"{artifact}={version}")
        if version != pyproject_version:
            print(
                f"エラー: {artifact} と pyproject.toml が不一致: {version} != {pyproject_version}",
                file=sys.stderr,
            )
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
