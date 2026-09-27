#!/usr/bin/env python3
# M7-2: 計測スクリプト。異なるチャンク分割方式と圧縮の組み合わせで、
# 増分容量と処理時間を計測する(要件定義書 T-1, T-2)。

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

# bvc のモジュールを import するため、src/ を sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bvc.repo import Repo

# 計測対象の組み合わせ
CHUNKERS: Final[list[dict]] = [
    {"name": "fixed", "size": 1 << 20},  # 1 MiB
    {"name": "fixed", "size": 1 << 22},  # 4 MiB(既定)
    {"name": "fixed", "size": 1 << 24},  # 16 MiB
    {
        "name": "gear",
        "min": 1 << 12,
        "avg": 1 << 14,
        "max": 1 << 18,
        "seed": 0x1234567890ABCDEF,
    },  # 4KB-16KB
    {
        "name": "gear",
        "min": 1 << 20,
        "avg": 1 << 22,
        "max": 1 << 26,
        "seed": 0xFEDCBA9876543210,
    },  # 1MB-64MB
]

COMPRESSIONS: Final[list[str]] = ["none", "zlib", "auto"]


def generate_test_data(size: int, variation: int = 0) -> bytes:
    # テスト用のバイナリデータ生成。variation で内容を変える(同じサイズでも異なる内容)。
    data = bytearray()
    for i in range(size):
        # シード値から決定的にデータを生成(同じ入力なら同じ出力)
        byte_val = ((i * 37 + variation * 101) ^ (i >> 8) ^ (variation >> 8)) & 0xFF
        data.append(byte_val)
    return bytes(data)


def measure_combination(
    workdir: Path,
    chunker: dict,
    compression: str,
    data_sizes: list[int],
) -> dict:
    # 1つの (chunker, compression) 組み合わせを計測する。
    # data_sizes: 各世代のファイルサイズ
    results = {"chunker": chunker, "compression": compression}
    times = []
    sizes = []

    try:
        # init
        start = time.perf_counter()
        with Repo.init(
            workdir, track=["data.bin"], chunker=chunker, compression=compression
        ) as repo:
            pass
        elapsed = time.perf_counter() - start
        times.append(elapsed)

        # 各世代でコミット
        for gen, size in enumerate(data_sizes):
            data = generate_test_data(size, variation=gen)
            (workdir / "data.bin").write_bytes(data)

            start = time.perf_counter()
            with Repo.open(workdir) as repo:
                repo.commit(f"gen {gen + 1}: {size} bytes")
            elapsed = time.perf_counter() - start
            times.append(elapsed)

            # .bvc のサイズを計測
            bvc_size = sum(
                (workdir / ".bvc" / p).stat().st_size
                for p in (workdir / ".bvc").rglob("*")
                if p.is_file()
            )
            sizes.append(bvc_size)

        results["times"] = times
        results["total_time"] = sum(times)
        results["final_size"] = sizes[-1] if sizes else 0
        results["total_data_size"] = sum(data_sizes)
        results["error"] = None
    except Exception as e:
        results["error"] = str(e)

    return results


def main():
    parser = argparse.ArgumentParser(description="計測スクリプト(M7-2)")
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[1000000, 1200000, 1100000, 1300000, 1050000],
        help="各世代のファイルサイズ(バイト)",
    )
    parser.add_argument(
        "--chunker",
        help="特定の chunker だけ計測(デフォルトはすべて)",
    )
    parser.add_argument(
        "--compression",
        help="特定の圧縮だけ計測(デフォルトはすべて)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON で結果を保存",
    )
    args = parser.parse_args()

    print(f"計測設定: {len(args.sizes)} 世代、ファイルサイズ {args.sizes}")
    print(f"計測対象: chunker {len(CHUNKERS)}, compression {len(COMPRESSIONS)}")
    print()

    chunkers = (
        CHUNKERS
        if args.chunker is None
        else [c for c in CHUNKERS if c["name"] == args.chunker]
    )
    compressions = COMPRESSIONS if args.compression is None else [args.compression]

    results = []
    for i, chunker in enumerate(chunkers):
        for j, compression in enumerate(compressions):
            print(
                f"[{i + 1}/{len(chunkers)} x {j + 1}/{len(compressions)}] ",
                end="",
                flush=True,
            )
            print(f"{chunker['name']} + {compression}...", end="", flush=True)

            # 一時フォルダを作成
            with tempfile.TemporaryDirectory() as tmpdir:
                result = measure_combination(
                    Path(tmpdir), chunker, compression, args.sizes
                )
                results.append(result)
                if result["error"]:
                    print(f" ERROR: {result['error']}")
                else:
                    print(
                        f" {result['final_size'] / 1048576:.2f} MB, "
                        f"{result['total_time']:.2f}s"
                    )

    # 結果をサマリーで表示
    print("\n結果サマリー:")
    print("-" * 80)
    print(f"{'Chunker':<30} {'Compression':<12} {'Size (MB)':<15} {'Time (s)':<12}")
    print("-" * 80)
    for r in results:
        if not r["error"]:
            chunker_name = f"{r['chunker']['name']}"
            if r["chunker"]["name"] == "fixed":
                chunker_name += f" ({r['chunker']['size'] / (1 << 20):.0f}MB)"
            elif r["chunker"]["name"] == "gear":
                avg = r["chunker"]["avg"] / 1024
                chunker_name += f" ({avg:.0f}KB)"
            print(
                f"{chunker_name:<30} {r['compression']:<12} "
                f"{r['final_size'] / 1048576:>10.2f} MB  {r['total_time']:>10.2f}s"
            )

    if args.output:
        args.output.write_text(json.dumps(results, indent=2))
        print(f"\n結果を {args.output} に保存しました")


if __name__ == "__main__":
    main()
