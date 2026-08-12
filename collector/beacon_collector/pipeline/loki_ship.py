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


# Loki's distributor->ingester hop is gRPC, and its default
# grpc_server_max_recv_msg_size is 4 MiB. A larger push is refused with
# HTTP 500 "ResourceExhausted", the batch is requeued at the same size, and it
# retries forever — a poison payload that never drains and blocks everything
# behind it. Four devices produce ~5.6 MB per 3 s flush, so this is the normal
# case, not an edge one. Split below the limit with room for JSON escaping.
MAX_PAYLOAD_BYTES = 3_200_000
_SIZE_ERRORS = (b"ResourceExhausted", b"larger than max", b"too large")


def _payloads(streams: dict, max_bytes: int = MAX_PAYLOAD_BYTES) -> list[bytes]:
    """Serialize accumulated streams into payloads each under max_bytes.

    Splits between streams and, when one stream alone is too big, within its
    values. Timestamps stay ascending per stream, which Loki requires, because
    values are sorted before chunking and chunks preserve that order.
    """
    out: list[bytes] = []
    cur: list[dict] = []
    size = 2                                  # {"streams":[]}
    for key, values in streams.items():
        label = {"serial": key[0], "source": key[1], "level": key[2]}
        overhead = len(json.dumps(label)) + 30
        i, vals = 0, sorted(values, key=lambda x: x[0])
        while i < len(vals):
            bucket: list = []
            bsize = overhead
            while i < len(vals):
                ts, line = vals[i]
                vsize = len(ts) + len(line) + 10
                if bucket and size + bsize + vsize > max_bytes:
                    break
                bucket.append(vals[i])
                bsize += vsize
                i += 1
            cur.append({"stream": label, "values": bucket})
            size += bsize
            if i < len(vals) or size >= max_bytes:
                out.append(json.dumps({"streams": cur}).encode())
                cur, size = [], 2
    if cur:
        out.append(json.dumps({"streams": cur}).encode())
    return out


class LokiShipper:
    def __init__(self, base_url: str, spool_dir, flush_interval: float = 3.0,
                 max_payload_bytes: int = MAX_PAYLOAD_BYTES):
        self.url = base_url.rstrip("/") + "/loki/api/v1/push"
        self.flush_interval = flush_interval
        self.max_payload_bytes = max_payload_bytes
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
        for payload in _payloads(streams, self.max_payload_bytes):
            if not await self._post(payload):
                self.pending.put(payload)

    async def _drain_pending(self, limit: int = 20):
        for p in self.pending.items()[:limit]:
            data = p.read_bytes()
            if await self._post(data):
                p.unlink(missing_ok=True)
                continue
            # An oversized payload spooled before the split existed can never
            # succeed as-is, so retrying it forever starves everything behind
            # it. Re-split it once and requeue the pieces.
            if len(data) > self.max_payload_bytes and self._resplit(p, data):
                continue
            return

    def _resplit(self, path, data: bytes) -> bool:
        try:
            doc = json.loads(data)
            streams = {
                (s["stream"]["serial"], s["stream"]["source"], s["stream"]["level"]):
                    s["values"] for s in doc["streams"]
            }
        except Exception:
            log.warning("dropping unparsable %d-byte pending loki payload "
                        "(lines remain in Parquet)", len(data))
            path.unlink(missing_ok=True)
            return True
        parts = _payloads(streams, self.max_payload_bytes)
        log.info("re-split a %d-byte pending loki payload into %d",
                 len(data), len(parts))
        for part in parts:
            self.pending.put(part)
        path.unlink(missing_ok=True)
        return True

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
            if r.status_code not in (200, 204):
                # Anything else used to fail silently, so a persistent 500 looked
                # like nothing at all while the batch retried forever. The body
                # is where the reason lives (out-of-order stream, too many
                # streams, label problems) — surface it.
                log.warning("loki push failed: HTTP %s %s",
                            r.status_code, r.text[:200].replace("\n", " "))
                return False
            return True
        try:
            return await asyncio.to_thread(_send)
        except Exception as e:
            log.debug("loki push failed: %s", e)
            return False
