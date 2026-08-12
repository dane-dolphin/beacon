from __future__ import annotations

import asyncio
import logging
import time

from . import onboarding, recorder, streams
from .adb import Adb
from .config import Config, DeviceConfig
from .parsers import dmesg as dmesg_parser
from .parsers.recorder_line import TOP_PREFIX, parse_recorder_line, parse_top_line
from .pipeline.metrics import (EventCounters, VMWriter, line, sample_to_lines,
                               top_to_lines)
from .pipeline.spool import RawSpool
from .registry import Registry

log = logging.getLogger(__name__)


class DeviceSupervisor:
    """Per-device connection state machine (§M0.3):

    connect -> root -> verify mali debugfs -> onboard/verify logd -> capture
    boot_epoch -> launch detached recorder -> spawn streams -> on any stream
    death, tear down and reconnect with backoff. A silence that ends with a
    reset uptime is a reboot, never a freeze (§1.2).
    """

    def __init__(self, adb: Adb, dev: DeviceConfig, cfg: Config, registry: Registry,
                 spool: RawSpool, vm: VMWriter, counters: EventCounters,
                 processor=None):
        self.adb = adb
        self.dev = dev
        self.cfg = cfg
        self.registry = registry
        self.spool = spool
        self.vm = vm
        self.counters = counters
        self.processor = processor  # M2 hook: .feed(serial, source, ts, line)

        # per-boot stream cursors
        self.last_uptime = 0.0
        self.last_logcat_ts: str | None = None
        self.last_dmesg_mono = 0.0
        self.last_rn_receive = 0.0
        self.boot_epoch: float | None = None
        self._had_boot = False
        # unreachable-device accounting (§TODO P1): without this a dead device
        # is completely silent — D-005-02408 was gone 13 h on 2026-07-29 and
        # beacon.log said nothing, because _connect_cycle returns False with no
        # log line and Adb.connect logs its failure at DEBUG.
        self._fail_count = 0
        self._down_since: float | None = None
        self._skipped = False

    @property
    def app_package(self) -> str:
        """Per-device override, falling back to the global. A discovered or
        non-Dolphin device set to "" tails no app log at all."""
        if self.dev.app_package is None:
            return self.cfg.app_package
        return self.dev.app_package

    def _refresh_overrides(self) -> bool:
        """Pull operator edits from the registry. Called at the top of every
        cycle so a change in the console lands within one backoff (<=60 s)
        rather than needing a collector restart. Returns True if this device
        is currently skipped."""
        try:
            o = self.registry.overrides().get(self.dev.serial)
        except Exception:
            log.debug("%s: could not read overrides", self.dev.serial)
            return False
        if not o:
            return False
        if o["friendly_name"]:
            self.dev.friendly_name = o["friendly_name"]
        if o["app_package"] is not None:
            self.dev.app_package = o["app_package"]
        if o["skip"] != self._skipped:
            log.info("%s: %s by operator override", self.dev.serial,
                     "skipped" if o["skip"] else "un-skipped")
            self._skipped = o["skip"]
        return o["skip"]

    async def run(self):
        backoff = 2.0
        while True:
            if self._refresh_overrides():
                # skipped: hold without connecting, and re-check periodically so
                # un-skipping also takes effect with no restart
                await asyncio.sleep(30)
                continue
            try:
                ok = await self._connect_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: supervisor cycle error", self.dev.serial)
                ok = False
            self._note_outcome(ok)
            backoff = 2.0 if ok else min(backoff * 2, self.cfg.reconnect_backoff_max)
            await asyncio.sleep(backoff)

    def _note_outcome(self, ok: bool):
        """Make an unreachable device visible, in the log and as a series."""
        now = time.monotonic()
        if ok:
            if self._down_since is not None:
                log.info("%s: reachable again after %.0fs (%d failed attempts)",
                         self.dev.serial, now - self._down_since, self._fail_count)
            self._fail_count = 0
            self._down_since = None
        else:
            if self._down_since is None:
                self._down_since = now
            self._fail_count += 1
            down = now - self._down_since
            # every attempt while young, then throttle: a device down for hours
            # should not write a line every 60 s
            if self._fail_count <= 3 or self._fail_count % 10 == 0:
                log.warning("%s: unreachable at %s (%d consecutive failures, "
                            "down %.0fs)", self.dev.serial, self.dev.address,
                            self._fail_count, down)
        self.vm.enqueue([line("stick_device_down_seconds", {"serial": self.dev.serial},
                              0.0 if ok else now - (self._down_since or now),
                              time.time())])

    async def _connect_cycle(self) -> bool:
        """One connected session. Returns True if we got as far as streaming
        (so the next backoff restarts small)."""
        d = self.dev
        if not await self.adb.connect(d.address):
            return False
        if not await self.adb.root(d.address):
            log.warning("%s: adb root failed — mali/dmesg unavailable, retrying", d.serial)
            return False

        serial = (await self.adb.shell(d.address, "getprop", "ro.serialno")).strip()
        if serial and serial != d.serial:
            log.warning("%s: device at %s reports serial %s — check registry",
                        d.serial, d.address, serial)
        self.registry.upsert_device(d.serial, d.address, d.nuc, d.friendly_name)

        # mali debugfs is required for the leak signal (§1.3)
        gpu = await self.adb.shell(d.address, "cat", "/sys/kernel/debug/mali0/gpu_memory")
        if "mali0" not in gpu:
            log.warning("%s: mali debugfs NOT readable — GPU series will be empty", d.serial)

        await self._establish_boot(d)

        state = self.registry.onboarding_state(d.serial)
        if not state.get("logd_verified_at"):
            report = await onboarding.onboard(self.adb, d.address, d.serial, self.registry)
            log.info("%s: onboarding report: %s", d.serial, report)

        await recorder.ensure_running(self.adb, d.address, self.app_package)

        log.info("%s: streaming (boot_epoch=%.0f)", d.serial, self.boot_epoch or 0)
        tasks = [
            asyncio.create_task(self._pump_recorder(), name=f"{d.serial}-rec"),
            asyncio.create_task(self._pump_logcat(), name=f"{d.serial}-logcat"),
            asyncio.create_task(self._pump_dmesg(), name=f"{d.serial}-dmesg"),
            asyncio.create_task(self._poll_vsync(), name=f"{d.serial}-vsync"),
            asyncio.create_task(self._heartbeat(), name=f"{d.serial}-hb"),
        ]
        # Gate at creation, NOT with an early return inside the pump: every
        # task here is watched with FIRST_COMPLETED, so a pump that returns
        # immediately tears down the whole session and spins a reconnect loop.
        if self.app_package:
            tasks.append(asyncio.create_task(self._pump_applog(),
                                             name=f"{d.serial}-applog"))
        # first stream to die means the adb session is gone: tear down all
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            log.info("%s: stream %s ended %s", d.serial, t.get_name(),
                     f"with {exc!r}" if exc else "cleanly (EOF)")
        log.info("%s: session ended; will reconnect", d.serial)
        self.spool.flush()
        return True

    async def _establish_boot(self, d: DeviceConfig):
        """Capture boot_epoch = wall_now - uptime per boot (§1.11); detect reboots."""
        up_out = await self.adb.shell(d.address, "cat", "/proc/uptime")
        host_now = time.time()
        uptime = float(up_out.split()[0])
        boot_epoch = host_now - uptime

        dev_now_out = await self.adb.shell(d.address, "date", "+%s")
        clock_offset = None
        try:
            clock_offset = host_now - float(dev_now_out.strip())
        except ValueError:
            pass

        # logcat prints device-LOCAL time; capture the UTC offset for the parser
        tz_raw = (await self.adb.shell(d.address, "date", "+%z")).strip()
        try:  # "-0500" -> -18000.0
            sign = -1.0 if tz_raw.startswith("-") else 1.0
            tz_offset = sign * (int(tz_raw[1:3]) * 3600 + int(tz_raw[3:5]) * 60)
        except (ValueError, IndexError):
            tz_offset = 0.0
        if self.processor:
            self.processor.set_tz(d.serial, tz_offset)

        reason = (await self.adb.shell(d.address, "getprop", "sys.boot.reason")).strip() or None
        is_new = self.registry.record_boot(d.serial, boot_epoch, reason, clock_offset)
        self.boot_epoch = self.registry.current_boot_epoch(d.serial) or boot_epoch

        if is_new and self._had_boot:
            # reboot: reset all per-boot cursors, count it, re-verify logd persistence
            log.info("%s: REBOOT detected (reason=%s)", d.serial, reason)
            self.last_uptime = 0.0
            self.last_logcat_ts = None
            self.last_dmesg_mono = 0.0
            # §6: collect observed reason values, don't hard-code a vocabulary
            self.counters.inc("stick_reboots_total",
                              {"serial": d.serial, "reason": reason or "unknown"})
            await onboarding.verify_after_reboot(self.adb, d.address, d.serial, self.registry)
        self._had_boot = True

    # ---- stream pumps ------------------------------------------------------

    async def _pump_recorder(self):
        d = self.dev
        async for raw in recorder.backfill_and_follow(self.adb, d.address, self.last_uptime):
            if raw.startswith(TOP_PREFIX):
                t = parse_top_line(raw)
                if t is None:
                    continue
                # NOT a last_uptime cursor update: that cursor belongs to the
                # 1 Hz tier, and moving it here would skip real samples.
                self.spool.append(d.serial, "rec", raw, ts=t.wall_ts(self.boot_epoch))
                self.vm.enqueue(top_to_lines(d.serial, t, self.boot_epoch))
                continue
            s = parse_recorder_line(raw)
            if s is None:
                continue
            self.last_uptime = s.uptime
            self.spool.append(d.serial, "rec", raw, ts=s.wall_ts(self.boot_epoch))
            self.vm.enqueue(sample_to_lines(d.serial, s, self.boot_epoch))

    async def _pump_logcat(self):
        d = self.dev
        async for raw in streams.logcat_lines(self.adb, d.address, self.last_logcat_ts):
            now = time.time()
            self.spool.append(d.serial, "logcat", raw, ts=now)
            # cursor for -T backfill on reconnect: device-clock prefix of the line
            if len(raw) > 18 and raw[2] == "-" and raw[5] == " ":
                self.last_logcat_ts = raw[:18]
            if " ReactNativeJS" in raw or "\tReactNativeJS" in raw:
                self.last_rn_receive = now
            if self.processor:
                self.processor.feed(d.serial, "logcat", now, raw)

    async def _pump_dmesg(self):
        d = self.dev
        async for mono, raw in streams.dmesg_lines(self.adb, d.address, self.last_dmesg_mono):
            self.last_dmesg_mono = mono
            wall = (self.boot_epoch or time.time() - mono) + mono
            self.spool.append(d.serial, "dmesg", raw, ts=wall)
            fault = dmesg_parser.gpu_fault_type(raw)
            if fault:
                self.counters.inc("stick_gpu_faults_total",
                                  {"serial": d.serial, "type": fault})
            if self.processor:
                self.processor.feed(d.serial, "dmesg", wall, raw)

    async def _pump_applog(self):
        d = self.dev
        async for raw in streams.app_log_lines(self.adb, d.address, self.app_package):
            now = time.time()
            self.spool.append(d.serial, "applog", raw, ts=now)
            if self.processor:
                self.processor.feed(d.serial, "applog", now, raw)

    async def _poll_vsync(self):
        d = self.dev
        while True:
            count = await streams.vsync_count(self.adb, d.address)
            if count is not None:
                self.vm.enqueue([line("stick_vsync_count_total",
                                      {"serial": d.serial}, count, time.time())])
            await asyncio.sleep(self.cfg.vsync_interval)

    async def _heartbeat(self):
        """stick_heartbeat_age_seconds: age of the newest ReactNativeJS line
        (§1.8). 30 s stall threshold is safe — median gap 2 ms, max 3 s."""
        d = self.dev
        while True:
            await asyncio.sleep(5)
            if self.last_rn_receive:
                age = time.time() - self.last_rn_receive
                self.vm.enqueue([line("stick_heartbeat_age_seconds",
                                      {"serial": d.serial}, age, time.time())])
