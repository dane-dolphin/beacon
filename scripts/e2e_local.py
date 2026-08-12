#!/usr/bin/env python3
"""End-to-end offline verification against the LOCAL docker stack.

Replays the measured fixtures (no device needed) through the real pipeline:
  recorder lines -> sample_to_lines -> VictoriaMetrics /write
  logcat lines   -> Processor (reassembly, tiers, derivations) -> Loki
then asserts the data is queryable back out of both stores.

Usage:  .venv/bin/python scripts/e2e_local.py
Needs:  docker compose stack up (deploy/), package installed in .venv
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

from beacon_collector.config import load_config  # noqa: E402
from beacon_collector.parsers.recorder_line import (parse_recorder_line,  # noqa: E402
                                                    parse_top_line)
from beacon_collector.pipeline.metrics import (EventCounters, VMWriter,  # noqa: E402
                                               sample_to_lines, top_to_lines)
from beacon_collector.pipeline.processor import Processor  # noqa: E402

FIX = ROOT / "collector" / "tests" / "fixtures"
SERIAL = "D-005-02408"


async def main() -> int:
    cfg = load_config(ROOT / "config" / "beacon.yaml")
    vm = VMWriter(cfg.victoriametrics, cfg.spool_dir)
    counters = EventCounters(vm)
    proc = Processor(cfg, vm, counters)

    now = time.time()

    # --- 1 Hz spine: recorder fixtures -> VM ---------------------------------
    rec_lines = [l for l in (FIX / "rec_sample.txt").read_text().splitlines()]
    samples = [s for s in map(parse_recorder_line, rec_lines) if s]
    boot_epoch = now - samples[-1].uptime - 1  # place samples just before "now"
    for s in samples:
        vm.enqueue(sample_to_lines(SERIAL, s, boot_epoch))
    # rec.sh v2 #top tier: per-process RAM/CPU on the same spine
    for t in (t for t in map(parse_top_line, rec_lines) if t):
        vm.enqueue(top_to_lines(SERIAL, t, boot_epoch))
    await vm.flush()

    # --- logcat fixtures -> processor -> Loki --------------------------------
    # Shift the fixtures' 'MM-DD HH' to one hour ago so the replay is
    # date-independent (device timestamps must stay recent for Loki).
    from datetime import datetime, timedelta, timezone
    stamp = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(hours=1)
    logcat_text = (FIX / "logcat_sample.txt").read_text().replace(
        "07-27 12", f"{stamp:%m-%d %H}")
    for raw in logcat_text.splitlines():
        proc.feed(SERIAL, "logcat", now, raw)
    for serial, st in proc._devices.items():
        for ev in st.reassembler.flush():
            proc._handle_event(serial, st, ev)
    for raw in (FIX / "dmesg_sample.txt").read_text().splitlines():
        proc.feed(SERIAL, "dmesg", now, raw)
    counters.publish()
    await proc.loki.flush()
    await vm.flush()

    time.sleep(8)  # VM -search.latencyOffset=5s: freshest points become visible
    failures = []

    # --- assertions -----------------------------------------------------------
    def vm_query(q):
        r = requests.get(f"{cfg.victoriametrics}/api/v1/query", params={"query": q}, timeout=10)
        return r.json()["data"]["result"]

    for q, desc in [
        (f'stick_gpu_pages{{serial="{SERIAL}",proc="dplayer"}}', "GPU pages (app)"),
        (f'stick_temp_celsius{{serial="{SERIAL}",zone="soc"}}', "SoC temperature"),
        (f'stick_mem_available_bytes{{serial="{SERIAL}"}}', "MemAvailable"),
        (f'stick_log_lines_total{{serial="{SERIAL}",level="E"}}', "E-line counter"),
        (f'last_over_time(stick_hdmi_connected{{serial="{SERIAL}"}}[6h])', "HDMI status (derived)"),
        (f'sum(stick_proc_pss_bytes{{serial="{SERIAL}"}})', "top-N PSS (stacked total)"),
        (f'count(stick_proc_cpu_seconds_total{{serial="{SERIAL}",mode="user"}})', "top-N processes seen"),
    ]:
        res = vm_query(q)
        status = "OK " if res else "FAIL"
        val = res[0]["value"][1] if res else "-"
        print(f"[{status}] VM  {desc:28s} = {val}")
        if not res:
            failures.append(f"VM: {desc}")

    start_ns = int((now - 6 * 3600) * 1e9)
    for sel, needle, desc in [
        (f'{{serial="{SERIAL}",source="logcat"}}', None, "filtered logcat lines"),
        (f'{{serial="{SERIAL}",source="events"}}', "creative", "creative-change events"),
        (f'{{serial="{SERIAL}",source="dmesg"}}', "DATA_INVALID_FAULT", "dmesg fault lines"),
    ]:
        r = requests.get(f"{cfg.loki}/loki/api/v1/query_range",
                         params={"query": sel, "start": start_ns, "limit": 100}, timeout=10)
        streams = r.json().get("data", {}).get("result", [])
        lines = [v[1] for s in streams for v in s["values"]]
        hit = any(needle in l for l in lines) if needle else bool(lines)
        status = "OK " if hit else "FAIL"
        print(f"[{status}] Loki {desc:27s} ({len(lines)} lines)")
        if not hit:
            failures.append(f"Loki: {desc}")

    if failures:
        print(f"\nE2E FAILED: {failures}")
        return 1
    print("\nE2E PASSED: fixtures flowed through VM and Loki and queried back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
