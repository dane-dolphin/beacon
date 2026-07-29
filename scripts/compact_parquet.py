#!/usr/bin/env python3
"""Nightly Parquet compaction (plan.md §3.6 rule 3).

Merges a day's hour-level objects into one day file per dataset:
    logs/dt=D/hour=HH/serial=*.parquet  ->  logs/dt=D/part-000.parquet
Hourly-per-device objects are ~0.3 MB; 175k tiny files per year would make
Athena crawl. Day files are ~160 MB at 20 devices — the sweet spot.

Deterministic and idempotent: rebuilding a day overwrites the same key.
Hour objects are removed only after the day file is written.

Usage: compact_parquet.py <parquet_root> [--date YYYY-MM-DD] [--keep-hours]
Defaults to yesterday (UTC). Run from cron on the NUC or the EC2 host.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow as pa

DATASETS = ("logs", "events", "metrics")


def compact_day(root: Path, dataset: str, date: str, keep_hours: bool) -> int:
    day_dir = root / dataset / f"dt={date}"
    hour_files = sorted(day_dir.glob("hour=*/serial=*.parquet"))
    if not hour_files:
        return 0
    tables = [pq.read_table(f) for f in hour_files]
    merged = pa.concat_tables(tables).sort_by("ts")
    out = day_dir / "part-000.parquet"
    pq.write_table(merged, out, compression="zstd")
    print(f"{dataset}/dt={date}: {len(hour_files)} hour objects -> "
          f"{out.name} ({merged.num_rows} rows)")
    if not keep_hours:
        for f in hour_files:
            f.unlink()
        for d in sorted(day_dir.glob("hour=*")):
            if not any(d.iterdir()):
                d.rmdir()
    return merged.num_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--date",
                    default=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--keep-hours", action="store_true",
                    help="do not delete hour objects after merging")
    args = ap.parse_args()

    total = 0
    for ds in DATASETS:
        total += compact_day(args.root, ds, args.date, args.keep_hours)
    if total == 0:
        print(f"nothing to compact for {args.date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
