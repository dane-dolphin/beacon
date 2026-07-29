from __future__ import annotations

import asyncio
import json
import logging

import requests

from .spool import PendingQueue

log = logging.getLogger(__name__)

# Ships filtered text to Loki's push API (§2.4 interactive tier, 30 d).
# Label set is deliberately tiny — {serial, source, level} — Loki cardinality
# rules are the same as VM's (§5.1); everything else is line content.


class LokiShipper:
    def __init__(self, base_url: str, spool_dir, flush_interval: float = 3.0,
                 batch_max: int = 2000):
        self.url = base_url.rstrip("/") + "/loki/api/v1/push"
        self.flush_interval = flush_interval
        self.batch_max = batch_max
        self.pending = PendingQueue(spool_dir, "loki")
        # {(serial, source, level): [[ts_ns, line], ...]}
        self._streams: dict[tuple, list] = {}
        self._count = 0

    def push(self, serial: str, source: str, level: str, ts: float, line: str):
        key = (serial, source, level)
        self._streams.setdefault(key, []).append([str(int(ts * 1e9)), line])
        self._count += 1

    def push_event(self, serial: str, ts: float, kind: str, payload: dict):
        """Structured event rows for Grafana annotations: one JSON line on the
        source='events' stream (creative_change, gpu_fault, reboot...)."""
        rec = {"kind": kind, **payload}
        self.push(serial, "events", "I", ts, json.dumps(rec, ensure_ascii=False))

    async def run(self):
        while True:
            await asyncio.sleep(self.flush_interval)
            await self.flush()
            await self._drain_pending()

    async def flush(self):
        if not self._streams:
            return
        streams, self._streams, self._count = self._streams, {}, 0
        payload = json.dumps({
            "streams": [
                {
                    "stream": {"serial": k[0], "source": k[1], "level": k[2]},
                    # Loki requires ascending timestamps per stream
                    "values": sorted(v, key=lambda x: x[0]),
                }
                for k, v in streams.items()
            ]
        }).encode()
        if not await self._post(payload):
            self.pending.put(payload)

    async def _drain_pending(self, limit: int = 20):
        for p in self.pending.items()[:limit]:
            if await self._post(p.read_bytes()):
                p.unlink(missing_ok=True)
            else:
                return

    async def _post(self, payload: bytes) -> bool:
        def _send():
            r = requests.post(self.url, data=payload,
                              headers={"Content-Type": "application/json"}, timeout=10)
            # 400 timestamp rejections (too old / too new / out of order) are
            # permanent for that payload: drop it — the raw data is still in
            # Parquet — rather than retry forever.
            if r.status_code == 400 and any(
                    s in r.content for s in
                    (b"too far behind", b"too new", b"out of order", b"greater than")):
                log.warning("loki rejected entries by timestamp (kept in Parquet): %s",
                            r.text[:200])
                return True
            return r.status_code in (200, 204)
        try:
            return await asyncio.to_thread(_send)
        except Exception as e:
            log.debug("loki push failed: %s", e)
            return False
