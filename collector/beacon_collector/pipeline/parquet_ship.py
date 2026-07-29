from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..parsers.logcat import parse_logcat_line

log = logging.getLogger(__name__)

# §3.5/§3.6 — Parquet holds EVERYTHING, unfiltered, forever. Idempotency by
# construction: one object per (dataset, day, hour, serial) at a key derived
# from the data, so a retry or reprocess OVERWRITES the same key instead of
# appending duplicates. Layout is hive-style so Athena needs only one
# CREATE EXTERNAL TABLE later; DuckDB reads it with hive_partitioning=1.
#
# Input is the RawSpool's hour-partitioned JSONL: spool/<serial>/<source>/
# dt=YYYY-MM-DD/hour=HH.jsonl. A closed hour (wall clock has moved past it)
# is converted once and marked with a .done sidecar.

LOGS_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us", tz="UTC")),          # real INT64 timestamp (§3.6 rule 4)
    ("serial", pa.string()),
    ("source", pa.string()),
    ("pid", pa.int32()),
    ("tid", pa.int32()),
    ("level", pa.string()),
    ("tag", pa.string()),
    ("message", pa.string()),
    ("raw", pa.string()),
])

EVENTS_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us", tz="UTC")),
    ("end_ts", pa.timestamp("us", tz="UTC")),
    ("serial", pa.string()),
    ("kind", pa.string()),
    ("payload", pa.string()),                      # JSON blob; low row count
])


def _us(ts: float) -> int:
    return int(ts * 1_000_000)


class ParquetShipper:
    def __init__(self, spool_dir: str | Path, parquet_dir: str | Path,
                 s3_bucket: str | None = None, s3_region: str = "us-east-1",
                 scan_interval: float = 300.0):
        self.spool = Path(spool_dir)
        self.out = Path(parquet_dir)
        self.s3_bucket = s3_bucket
        self.s3_region = s3_region
        self.scan_interval = scan_interval
        self._events: list[dict] = []   # buffered event rows, flushed hourly

    # ---- events (creative intervals, faults, reboots) -----------------------

    def add_event(self, serial: str, ts: float, kind: str, payload: dict,
                  end_ts: float | None = None):
        self._events.append({
            "ts": ts, "end_ts": end_ts, "serial": serial,
            "kind": kind, "payload": json.dumps(payload, ensure_ascii=False),
        })

    # ---- main loop -----------------------------------------------------------

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.process_closed_hours)
                await asyncio.to_thread(self.flush_events)
            except Exception:
                log.exception("parquet shipper cycle failed")
            await asyncio.sleep(self.scan_interval)

    # ---- logs: spool JSONL hour files -> parquet ----------------------------

    def process_closed_hours(self, now: float | None = None):
        import time as _t
        now = now or _t.time()
        for jsonl in self.spool.glob("*/*/dt=*/hour=*.jsonl"):
            done = jsonl.with_suffix(".jsonl.done")
            if done.exists():
                continue
            serial, source = jsonl.parts[-4], jsonl.parts[-3]
            dt = jsonl.parts[-2].split("=", 1)[1]
            hour = jsonl.stem.split("=", 1)[1]
            # closed = wall clock is past the end of that hour (UTC)
            from datetime import datetime, timezone
            hour_end = datetime.fromisoformat(f"{dt}T{hour}:00:00+00:00").timestamp() + 3600
            if now < hour_end + 60:
                continue
            key = f"logs/dt={dt}/hour={hour}/serial={serial}-{source}.parquet"
            self._convert_log_hour(jsonl, serial, source, key)
            done.touch()

    def _convert_log_hour(self, jsonl: Path, serial: str, source: str, key: str):
        rows = {name: [] for name in LOGS_SCHEMA.names}
        seen: set[tuple] = set()
        with open(jsonl, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts, line = rec.get("ts"), rec.get("line", "")
                if ts is None:
                    continue
                pid = tid = None
                level = tag = None
                if source == "logcat":
                    p = parse_logcat_line(line, ts)
                    if p:
                        pid, tid, level, tag = p.pid, p.tid, p.level, p.tag
                        msg = p.message
                    else:
                        msg = line
                    # reconnect backfill via -T can duplicate boundary lines;
                    # the raw text is the identity
                    k = (line,)
                    if k in seen:
                        continue
                    seen.add(k)
                else:
                    msg = line
                rows["ts"].append(_us(ts))
                rows["serial"].append(serial)
                rows["source"].append(source)
                rows["pid"].append(pid)
                rows["tid"].append(tid)
                rows["level"].append(level)
                rows["tag"].append(tag)
                rows["message"].append(msg)
                rows["raw"].append(line)
        if not rows["ts"]:
            return
        table = pa.table(
            {n: pa.array(rows[n], type=LOGS_SCHEMA.field(n).type) for n in LOGS_SCHEMA.names},
            schema=LOGS_SCHEMA)
        self._write(table, key)
        log.info("parquet: %s (%d rows)", key, table.num_rows)

    # ---- events -------------------------------------------------------------

    def flush_events(self):
        """Group buffered events by (day, hour, serial); rewrite each affected
        object from ALL rows buffered for it (deterministic key, §3.5)."""
        if not self._events:
            return
        from datetime import datetime, timezone
        groups: dict[str, list[dict]] = {}
        for e in self._events:
            d = datetime.fromtimestamp(e["ts"], tz=timezone.utc)
            key = f"events/dt={d:%Y-%m-%d}/hour={d:%H}/serial={e['serial']}.parquet"
            groups.setdefault(key, []).append(e)
        self._events = []
        for key, rows in groups.items():
            # merge with an existing object for the same hour if present
            existing = self.out / key
            tables = []
            if existing.exists():
                tables.append(pq.read_table(existing))
            tables.append(pa.table({
                "ts": pa.array([_us(r["ts"]) for r in rows], type=EVENTS_SCHEMA.field("ts").type),
                "end_ts": pa.array([_us(r["end_ts"]) if r["end_ts"] else None for r in rows],
                                   type=EVENTS_SCHEMA.field("end_ts").type),
                "serial": [r["serial"] for r in rows],
                "kind": [r["kind"] for r in rows],
                "payload": [r["payload"] for r in rows],
            }, schema=EVENTS_SCHEMA))
            self._write(pa.concat_tables(tables), key)

    # ---- output -------------------------------------------------------------

    def _write(self, table: pa.Table, key: str):
        local = self.out / key
        local.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, local, compression="zstd")   # §3.6 rule 5
        if self.s3_bucket:
            try:
                import boto3
                boto3.client("s3", region_name=self.s3_region).upload_file(
                    str(local), self.s3_bucket, key)
            except Exception:
                log.exception("s3 upload failed for %s (kept locally)", key)
