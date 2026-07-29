from __future__ import annotations

import asyncio
import logging
import re

from .adb import Adb

log = logging.getLogger(__name__)

APP_LOG_DIR = "/sdcard/Android/data/{pkg}/files/Logs"
APP_LOG_FILE = "Dolphin_File.log"


async def logcat_lines(adb: Adb, address: str, since: str | None):
    """Stream `logcat -v threadtime -b all`.

    since: a device-clock 'MM-DD HH:MM:SS.mmm' string from the last line seen
    this boot. On reconnect we pass it via -T so the 4 MiB ring buffers
    (§1.7) backfill the disconnect gap instead of losing it. First attach of
    a boot dumps the whole buffer (since=None).

    The -T value MUST carry quotes that survive to the device. adb shell
    joins its args with spaces and the DEVICE shell re-splits them, so a
    bare timestamp arrives as `-T 07-28` + a stray `23:30:00.000` and logcat
    exits immediately with "not in time format" (same gotcha as the compound
    commands in recorder.ensure_running). That EOF kills the session, and
    since every reconnect re-sends -T, the supervisor spins in a reconnect
    loop collecting nothing.
    """
    cmd = ["shell", "logcat", "-v", "threadtime", "-b", "all"]
    if since:
        cmd += ["-T", f"'{since}'"]
    async with adb.stream(address, *cmd) as s:
        async for line in s.lines():
            yield line


_DMESG_TS = re.compile(r"^\[\s*(\d+\.\d+)\]")


async def dmesg_lines(adb: Adb, address: str, last_monotonic: float):
    """Stream `dmesg -w` (root). The whole kernel ring re-dumps on every
    attach, so lines at or before last_monotonic (same boot) are dropped
    here. Timestamps are kernel-monotonic — the caller converts to wall
    clock with boot_epoch (§1.11); the bogus wall-clock in logcat's kernel
    buffer is never used."""
    async with adb.stream(address, "shell", "dmesg", "-w") as s:
        async for line in s.lines():
            m = _DMESG_TS.match(line)
            if m:
                ts = float(m.group(1))
                if ts <= last_monotonic:
                    continue
                yield ts, line
            # non-timestamped continuation lines are skipped (rare)


async def app_log_lines(adb: Adb, address: str, pkg: str, backlog_lines: int = 200):
    """Follow the app's own file log (§1.15) — written directly by the app,
    so chatty never touched it. Backfill across disconnects is bounded by
    backlog_lines; the complete files are pulled wholesale during incident
    forensics (M4).

    The device's toybox tail has no -F, so we follow with -f and reopen when
    the app rotates its file (tail goes silent on the old fd, or exits if
    the file is momentarily absent). This generator never ends on its own;
    a dead connection makes the OTHER streams die and the supervisor tears
    the session down, cancelling this one.
    """
    path = f"{APP_LOG_DIR.format(pkg=pkg)}/{APP_LOG_FILE}"
    n = str(backlog_lines)
    while True:
        got_any = False
        async with adb.stream(address, "shell", "tail", "-f", "-n", n, path) as s:
            async for line in s.lines(idle_timeout=600.0):
                got_any = True
                yield line
        log.info("%s: app log tail ended (%s) — reopening",
                 address, "idle" if got_any else "EOF")
        await asyncio.sleep(5.0)
        n = "0"  # after the first attach, don't re-emit backlog on reopen


async def vsync_count(adb: Adb, address: str) -> int | None:
    """SurfaceFlinger vsync counter (§1.14) — liveness, 10 s cadence."""
    out = await adb.shell(address, "dumpsys", "SurfaceFlinger", timeout=15.0)
    for m in re.finditer(r"count=(\d+)", out):
        return int(m.group(1))
    return None
