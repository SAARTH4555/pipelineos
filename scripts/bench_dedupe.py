"""Benchmark contact dedupe on generated data: wall time, peak memory, comparisons vs naive.

Usage: uv run python scripts/bench_dedupe.py --n 10000 [--out results/bench.json]
Generation is excluded from the timing; normalization, matching and merging are included.
"""

from __future__ import annotations

import argparse
import json
import platform
import time
import tracemalloc
from pathlib import Path

from pipelineos.data.dedupe import dedupe_contacts
from pipelineos.data.generate import GenConfig, generate


def bench(n: int, seed: int, repeats: int) -> dict[str, object]:
    data, summary = generate(GenConfig(n=n, dirty_fraction=0.2, seed=seed))
    rows = data.contacts
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = dedupe_contacts(rows)
        timings.append(time.perf_counter() - started)
    tracemalloc.start()
    dedupe_contacts(rows)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stats = result.stats
    return {
        "people": n,
        "rows": len(rows),
        "seconds_best": round(min(timings), 3),
        "seconds_median": round(sorted(timings)[len(timings) // 2], 3),
        "peak_memory_mb": round(peak / 1_000_000, 1),
        "comparisons": stats.comparisons,
        "naive_comparisons": stats.rows * (stats.rows - 1) // 2,
        "blocks": stats.blocks,
        "largest_block": stats.largest_block,
        "golden_out": len(result.golden),
        "duplicate_rows_generated": summary["duplicate_rows"],
        "machine": f"{platform.machine()} / Python {platform.python_version()}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, nargs="+", default=[1000, 3000, 10000])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    runs = [bench(n, args.seed, args.repeats) for n in args.n]
    for run in runs:
        print(
            f"{run['rows']:>6} rows: {run['seconds_best']:.3f}s, {run['peak_memory_mb']} MB peak, "
            f"{run['comparisons']:,} comparisons vs {run['naive_comparisons']:,} naive"
        )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"runs": runs}, indent=2) + "\n")


if __name__ == "__main__":
    main()
