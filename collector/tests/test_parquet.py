import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from beacon_collector.pipeline.parquet_ship import ParquetShipper
from beacon_collector.pipeline.spool import RawSpool

FIX = Path(__file__).parent / "fixtures"

TS = datetime(2026, 7, 27, 14, 10, 0, tzinfo=timezone.utc).timestamp()
AFTER_HOUR = datetime(2026, 7, 27, 15, 5, 0, tzinfo=timezone.utc).timestamp()


def _fill_spool(spool_dir):
    spool = RawSpool(spool_dir)
    for i, raw in enumerate((FIX / "logcat_sample.txt").read_text().splitlines()):
        spool.append("D-005-02408", "logcat", raw, ts=TS + i)
    spool.close()


def test_closed_hour_becomes_parquet_deterministically(tmp_path):
    spool_dir, out_dir = tmp_path / "spool", tmp_path / "parquet"
    _fill_spool(spool_dir)
    shipper = ParquetShipper(spool_dir, out_dir)

    # hour not closed yet -> nothing converted
    shipper.process_closed_hours(now=TS + 60)
    assert not list(out_dir.rglob("*.parquet"))

    shipper.process_closed_hours(now=AFTER_HOUR)
    key = out_dir / "logs/dt=2026-07-27/hour=14/serial=D-005-02408-logcat.parquet"
    assert key.exists()

    # §3.5: reprocessing overwrites the SAME key — no duplicate rows
    n1 = duckdb.sql(f"SELECT count(*) FROM '{key}'").fetchone()[0]
    for f in spool_dir.rglob("*.done"):
        f.unlink()
    shipper.process_closed_hours(now=AFTER_HOUR)
    n2 = duckdb.sql(f"SELECT count(*) FROM '{key}'").fetchone()[0]
    assert n1 == n2 > 0


def test_parquet_queryable_with_hive_partitioning_and_real_timestamps(tmp_path):
    spool_dir, out_dir = tmp_path / "spool", tmp_path / "parquet"
    _fill_spool(spool_dir)
    ParquetShipper(spool_dir, out_dir).process_closed_hours(now=AFTER_HOUR)

    rows = duckdb.sql(
        f"SELECT dt, level, tag, count(*) AS n "
        f"FROM read_parquet('{out_dir}/logs/*/*/*.parquet', hive_partitioning=1) "
        f"GROUP BY 1,2,3 ORDER BY n DESC"
    ).fetchall()
    assert rows, "no rows in parquet"
    tags = {r[2] for r in rows}
    assert "ReactNativeJS" in tags and "ocessService0" in tags

    # §3.6 rule 4: ts must be a real TIMESTAMP logical type, not a string
    (typ,) = duckdb.sql(
        f"SELECT typeof(ts) FROM read_parquet('{out_dir}/logs/*/*/*.parquet') LIMIT 1"
    ).fetchone()
    assert "TIMESTAMP" in typ.upper()


def test_event_rows_merge_per_hour(tmp_path):
    shipper = ParquetShipper(tmp_path / "spool", tmp_path / "parquet")
    shipper.add_event("D-005-02408", TS, "creative_interval",
                      {"creative_id": "A"}, end_ts=TS + 30)
    shipper.flush_events()
    shipper.add_event("D-005-02408", TS + 40, "creative_interval",
                      {"creative_id": "B"}, end_ts=TS + 70)
    shipper.flush_events()

    key = tmp_path / "parquet/events/dt=2026-07-27/hour=14/serial=D-005-02408.parquet"
    rows = duckdb.sql(f"SELECT kind, payload FROM '{key}' ORDER BY ts").fetchall()
    assert len(rows) == 2
    assert json.loads(rows[0][1])["creative_id"] == "A"
    assert json.loads(rows[1][1])["creative_id"] == "B"
