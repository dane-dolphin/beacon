from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from ..parsers import rn_fields
from ..parsers.logcat import parse_logcat_line
from ..parsers.reassemble import Reassembler
from ..pipeline.loki_ship import LokiShipper
from ..pipeline.metrics import EventCounters, VMWriter, line
from ..pipeline.parquet_ship import ParquetShipper
from ..pipeline.tiers import TierFilter

log = logging.getLogger(__name__)


def _sig_label(sig: str) -> str:
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


class _DeviceState:
    def __init__(self, serial: str):
        self.reassembler = Reassembler()
        self.tiers = TierFilter()
        self.creatives = rn_fields.CreativeTracker(serial)
        self.last_feed = 0.0


class Processor:
    """M2 pipeline: logical events -> tier filter -> Loki; derived tags ->
    metrics + event rows; everything raw is already in the spool for Parquet.
    Fed synchronously from the supervisor pumps via feed()."""

    def __init__(self, cfg, vm: VMWriter, counters: EventCounters, registry=None):
        self.cfg = cfg
        self.vm = vm
        self.counters = counters
        self.registry = registry
        self.loki = LokiShipper(cfg.loki, cfg.spool_dir)
        self.parquet = ParquetShipper(cfg.spool_dir, cfg.parquet_dir,
                                      cfg.s3_bucket, cfg.s3_region)
        self._devices: dict[str, _DeviceState] = {}
        # per-device UTC offset (`date +%z`) — logcat prints device-LOCAL time
        self.tz_offsets: dict[str, float] = {}

    def set_tz(self, serial: str, offset_s: float):
        self.tz_offsets[serial] = offset_s

    def _state(self, serial: str) -> _DeviceState:
        st = self._devices.get(serial)
        if st is None:
            st = self._devices[serial] = _DeviceState(serial)
        return st

    # ---- ingest -------------------------------------------------------------

    def feed(self, serial: str, source: str, ts: float, raw: str):
        try:
            if source == "logcat":
                self._feed_logcat(serial, ts, raw)
            elif source == "dmesg":
                # counters were incremented in the supervisor; text -> Loki
                self.loki.push(serial, "dmesg", "K", ts, raw)
            elif source == "applog":
                self._feed_applog(serial, ts, raw)
        except Exception:
            log.exception("processor feed failed for %s/%s", serial, source)

    def _feed_logcat(self, serial: str, ts: float, raw: str):
        st = self._state(serial)
        st.last_feed = time.monotonic()
        parsed = parse_logcat_line(raw, ts, self.tz_offsets.get(serial, 0.0))
        if parsed is None:
            if raw.strip() and not raw.startswith("-"):  # skip logcat buffer headers
                # §3.4 rule 2: unparsed ALWAYS ships, and its rate is a metric
                self.counters.inc("stick_unparsed_lines_total", {"serial": serial})
                self.loki.push(serial, "logcat", "unparsed", ts, raw)
            return
        self.counters.inc("stick_log_lines_total",
                          {"serial": serial, "level": parsed.level})
        for ev in st.reassembler.feed(parsed):
            self._handle_event(serial, st, ev)

    def _feed_applog(self, serial: str, ts: float, raw: str):
        # §1.15: strictly more complete than logcat, but its RN content
        # duplicates what tier3 already structured — ship only problem lines.
        if any(k in raw for k in ("ERROR", "WARN", "Exception", "FATAL")):
            self.loki.push(serial, "applog", "E", ts, raw)

    # ---- logical events -------------------------------------------------------

    def _handle_event(self, serial: str, st: _DeviceState, ev):
        first = ev.first
        d = st.tiers.classify(ev)

        if d.reason == "tier1":
            sig = _sig_label(d.dedup_sig)
            self.counters.inc("stick_errors_total", {"serial": serial, "sig": sig})
            if d.is_exemplar:
                self.loki.push(serial, "logcat", first.level, first.device_ts,
                               ev.text if ev.n_lines > 1 else first.raw)
            return

        if d.reason == "tier3":
            self._handle_rn_block(serial, st, ev)
            return

        if d.ship:
            self.loki.push(serial, "logcat", first.level, first.device_ts,
                           ev.text if ev.n_lines > 1 else first.raw)

        # single-line RN lines still carry derivable fields (hdmi, swaps, vc)
        if first.tag == "ReactNativeJS":
            self._derive(serial, st, first.device_ts, ev.text)

    def _handle_rn_block(self, serial: str, st: _DeviceState, ev):
        """§3.3 tier 3: reassembled JSON block -> one structured row; the
        20-30 physical lines are discarded from the interactive tier (they
        remain in Parquet via the raw spool)."""
        fields = self._derive(serial, st, ev.first.device_ts, ev.text)
        if fields:
            compact = {k: v for k, v in fields.items() if k != "webview_swap"}
            self.loki.push_event(serial, ev.first.device_ts, "app_block", compact)

    def _derive(self, serial: str, st: _DeviceState, ts: float, text: str) -> dict:
        fields = rn_fields.extract_fields(text)
        if not fields:
            return {}
        if "hdmi_connected" in fields:
            self.vm.enqueue([line("stick_hdmi_connected", {"serial": serial},
                                  int(fields["hdmi_connected"]), ts)])
        if self.registry and ("d_id" in fields or "installation_id" in fields):
            self.registry.update_identity(
                serial, d_id=fields.get("d_id"),
                venue_code=fields.get("venue_code"),
                installation_id=fields.get("installation_id"))

        closed = st.creatives.observe(ts, fields)
        cur = st.creatives.current
        if closed is not None:
            payload = {k: v for k, v in vars(closed).items() if k != "serial"}
            self.parquet.add_event(serial, closed.start_ts, "creative_interval",
                                   payload, end_ts=closed.end_ts)
        if cur is not None and cur.start_ts == ts:
            # a creative change happened right now -> annotation event
            self.loki.push_event(serial, ts, "creative_change", {
                "creative_id": cur.creative_id,
                "app_version": cur.app_version,
                "url": cur.url,
            })
        return fields

    # ---- background -----------------------------------------------------------

    async def run(self):
        tasks = [
            asyncio.create_task(self.loki.run(), name="loki-shipper"),
            asyncio.create_task(self.parquet.run(), name="parquet-shipper"),
            asyncio.create_task(self._flusher(), name="reassembler-flush"),
        ]
        await asyncio.gather(*tasks)

    async def _flusher(self):
        """Close logical events that have been idle — a block is complete when
        no continuation arrived for a moment (stream lulls, end of burst)."""
        while True:
            await asyncio.sleep(2.0)
            now = time.monotonic()
            for serial, st in self._devices.items():
                if st.last_feed and now - st.last_feed > 1.0:
                    for ev in st.reassembler.flush():
                        self._handle_event(serial, st, ev)
