from __future__ import annotations

import asyncio
import logging
import time

import requests

from ..parsers.recorder_line import RecorderSample
from .spool import PendingQueue

log = logging.getLogger(__name__)

# §5 metric schema. Emission is InfluxDB line protocol to VictoriaMetrics
# /write (run VM with -influxSkipSingleField so the metric name is the
# measurement, not measurement_value). Timestamps in nanoseconds, always
# derived from a full wall-clock epoch (§1.11).


def _esc(v: str) -> str:
    return str(v).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def line(measurement: str, labels: dict[str, str], value: float, ts: float) -> str:
    tags = "".join(f",{k}={_esc(v)}" for k, v in sorted(labels.items()))
    return f"{measurement}{tags} value={value} {int(ts * 1e9)}"


def sample_to_lines(serial: str, s: RecorderSample, boot_epoch: float,
                    app_label: str = "dplayer") -> list[str]:
    """RecorderSample -> §5 series. GPU pages are gauges — rollups elsewhere
    must keep max/min/last, never mean (§1.4); we store full 1 Hz so this is
    only a concern for future downsampling code."""
    ts = s.wall_ts(boot_epoch)
    L: list[str] = [line("stick_uptime_seconds", {"serial": serial}, s.uptime, ts)]
    if s.gpu_total_pages is not None:
        L.append(line("stick_gpu_pages_total", {"serial": serial}, s.gpu_total_pages, ts))
    if s.app_gpu_pages is not None:
        L.append(line("stick_gpu_pages", {"serial": serial, "proc": app_label}, s.app_gpu_pages, ts))
    if s.app_pid is not None:
        L.append(line("stick_app_pid", {"serial": serial, "pkg": app_label}, s.app_pid, ts))
    if s.mem_available_kb is not None:
        L.append(line("stick_mem_available_bytes", {"serial": serial}, s.mem_available_kb * 1024, ts))
    if s.cma_free_kb is not None:
        L.append(line("stick_cma_free_bytes", {"serial": serial}, s.cma_free_kb * 1024, ts))
    if s.temp_soc_milli is not None:
        L.append(line("stick_temp_celsius", {"serial": serial, "zone": "soc"}, s.temp_soc_milli / 1000.0, ts))
    if s.temp_ddr_milli is not None:
        L.append(line("stick_temp_celsius", {"serial": serial, "zone": "ddr"}, s.temp_ddr_milli / 1000.0, ts))
    if s.loadavg:
        for w, v in zip(("1m", "5m", "15m"), s.loadavg):
            L.append(line("stick_loadavg", {"serial": serial, "window": w}, v, ts))
    for n, (u, sy, idle) in enumerate(s.cpu):
        for mode, v in (("user", u), ("system", sy), ("idle", idle)):
            L.append(line("stick_cpu_jiffies_total", {"serial": serial, "cpu": str(n), "mode": mode}, v, ts))
    for iface, rx, tx in _named(s.net):
        L.append(line("stick_net_bytes_total", {"serial": serial, "iface": iface, "dir": "rx"}, rx, ts))
        L.append(line("stick_net_bytes_total", {"serial": serial, "iface": iface, "dir": "tx"}, tx, ts))
    return L


def _named(net: list) -> list[tuple[str, int, int]]:
    out = []
    for i, item in enumerate(net):
        if len(item) == 3:  # (iface, rx, tx)
            out.append((str(item[0]), int(item[1]), int(item[2])))
        else:
            out.append((f"net{i}", int(item[0]), int(item[1])))
    return out


class EventCounters:
    """Cumulative counters for event-derived series (§5): faults by type,
    log lines by level, reboots, unparsed lines, dedup counts. State is
    in-memory per collector run; VM treats a counter reset like any
    Prometheus counter reset.

    inc() only updates state; publish() emits every counter at one wall
    timestamp, Prometheus-scrape style. Emitting a point per increment is
    WRONG: bursts land on the same millisecond and last-write-wins can make
    the stored counter go backwards.
    """

    def __init__(self, writer: "VMWriter"):
        self.writer = writer
        self._counts: dict[tuple, float] = {}

    def inc(self, measurement: str, labels: dict[str, str], by: float = 1.0):
        key = (measurement, tuple(sorted(labels.items())))
        self._counts[key] = self._counts.get(key, 0.0) + by

    def publish(self):
        now = time.time()
        self.writer.enqueue([
            line(measurement, dict(labels), value, now)
            for (measurement, labels), value in self._counts.items()
        ])

    async def run(self, interval: float = 5.0):
        while True:
            await asyncio.sleep(interval)
            self.publish()


class VMWriter:
    """Batches line-protocol writes to VictoriaMetrics with a durable retry
    queue. Resends are harmless: same series+timestamp = last write wins."""

    def __init__(self, base_url: str, spool_dir, batch_max: int = 5000,
                 flush_interval: float = 5.0):
        self.url = base_url.rstrip("/") + "/write"
        self.batch: list[str] = []
        self.batch_max = batch_max
        self.flush_interval = flush_interval
        self.pending = PendingQueue(spool_dir, "vm")
        self._lock = asyncio.Lock()

    def enqueue(self, lines: list[str]):
        self.batch.extend(lines)

    async def run(self):
        """Periodic flush + retry of the durable queue."""
        while True:
            await asyncio.sleep(self.flush_interval)
            await self.flush()
            await self._drain_pending()

    async def flush(self):
        async with self._lock:
            if not self.batch:
                return
            payload = ("\n".join(self.batch)).encode()
            self.batch = []
        if not await self._post(payload):
            self.pending.put(payload)

    async def _drain_pending(self, limit: int = 20):
        for p in self.pending.items()[:limit]:
            if await self._post(p.read_bytes()):
                p.unlink(missing_ok=True)
            else:
                return  # endpoint still down; keep order, retry next cycle

    async def _post(self, payload: bytes) -> bool:
        def _send():
            r = requests.post(self.url, data=payload, timeout=10)
            return r.status_code in (200, 204)
        try:
            return await asyncio.to_thread(_send)
        except Exception as e:
            log.debug("VM write failed: %s", e)
            return False
