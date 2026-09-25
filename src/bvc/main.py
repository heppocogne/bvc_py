# エントリポイント。cli を呼び出して終了コードを返すだけにする(設計書 1.1節)。

import sys

from bvc import cli


def main(argv: list[str] | None = None) -> int:
    return cli.run(argv)


if __name__ == "__main__":
    sys.exit(main())
