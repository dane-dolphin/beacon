from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

from .adb import Adb
from .parsers.recorder_line import TOP_PREFIX

log = logging.getLogger(__name__)

REMOTE_SCRIPT = "/data/local/tmp/rec.sh"
REMOTE_DIR = "/data/local/tmp/beacon"
REMOTE_LOG = f"{REMOTE_DIR}/rec.log"

_LOCAL_SCRIPT = Path(__file__).resolve().parents[2] / "device" / "rec.sh"


async def ensure_running(adb: Adb, address: str, app_package: str) -> bool:
    """Push rec.sh if missing/stale and launch it detached (§1.12).

    Idempotent: rec.sh itself refuses to start a second instance. Called on
    every (re)connect — the recorder does not survive reboot, but does
    survive adb disconnects.
    """
    # NOTE: compound commands are passed as ONE argument. adb shell joins its
    # args with spaces and the DEVICE shell re-parses the result, so a local
    # 'sh -c "cmd"' wrapper arrives mangled as `sh -c cmd` + stray words.
    local = _LOCAL_SCRIPT.read_bytes()
    local_md5 = hashlib.md5(local).hexdigest()
    remote_md5 = (await adb.shell(address, "md5sum", REMOTE_SCRIPT)).split()[0:1]
    if not remote_md5 or remote_md5[0] != local_md5:
        if not await adb.push(address, _LOCAL_SCRIPT, REMOTE_SCRIPT):
            return False
        # script changed: stop any old instance so the new one takes over
        await adb.shell(
            address,
            f"[ -f {REMOTE_DIR}/rec.pid ] && kill $(cat {REMOTE_DIR}/rec.pid) 2>/dev/null; true")
    # the exact launch validated on hardware in §1.12
    await adb.shell(
        address,
        f"setsid sh {REMOTE_SCRIPT} {app_package} {REMOTE_DIR} >/dev/null 2>&1 </dev/null &")
    await asyncio.sleep(1.0)
    # verify liveness: the pidfile holds a running pid
    out = await adb.shell(
        address,
        f'p=$(cat {REMOTE_DIR}/rec.pid 2>/dev/null); [ -n "$p" ] && [ -d /proc/$p ] && echo alive')
    alive = "alive" in out
    if not alive:
        log.warning("%s: recorder failed to start", address)
    return alive


async def backfill_and_follow(adb: Adb, address: str, last_uptime: float,
                              boot_epoch: float | None = None):
    """Yield raw recorder lines: first everything newer than last_uptime from
    the rotated + current files (the disconnect gap), then follow live.

    Dedup is by device uptime, which is strictly increasing within a boot;
    after a reboot uptime resets below last_uptime, so pass last_uptime=0.
    The overlap between the cat and the tail is removed by the same rule.

    boot_epoch is what makes the follow safe across a reboot — see
    _is_stale_boot. Pass it whenever it is known; without it the follow keeps
    the old, unguarded behaviour.

    The device's toybox tail has no -F: after rec.sh rotates (mv + fresh
    append, §7) a plain -f goes silent on the old fd. The idle timeout
    catches that (the recorder writes ~1 line/s) and we loop back to a fresh
    backfill + follow. A dead connection surfaces here as a broken stream,
    which ends this generator — the supervisor owns reconnect policy.
    """
    seen = last_uptime
    # #top lines carry their own uptime and arrive ~30x less often, so they get
    # their own cursor. Sharing `seen` with the 1 Hz lines would drop whichever
    # kind lost the race at a given uptime.
    seen_top = last_uptime

    while True:
        dump = await adb.shell(
            address, f"cat {REMOTE_LOG}.1 {REMOTE_LOG} 2>/dev/null", timeout=120.0)
        for line in _current_boot_lines(dump.splitlines()):
            top_up = _top_uptime_of(line)
            if top_up is not None:
                if top_up > seen_top:
                    seen_top = top_up
                    yield line
                continue
            up = _uptime_of(line)
            if up is not None and up > seen:
                seen = up
                yield line

        got_any = False
        prev_up = None
        async with adb.stream(address, "shell", "tail", "-f", "-n", "50", REMOTE_LOG) as s:
            async for line in s.lines(idle_timeout=90.0):
                top_up = _top_uptime_of(line)
                if top_up is not None:
                    if _is_stale_boot(top_up, boot_epoch):
                        continue
                    if top_up > seen_top:
                        seen_top = top_up
                        got_any = True
                        yield line
                    continue
                up = _uptime_of(line)
                if up is None:
                    continue
                # Drop the previous boot's replay BEFORE it can touch prev_up,
                # the cursor, or the ingest path.
                if _is_stale_boot(up, boot_epoch):
                    continue
                if prev_up is not None and up < prev_up:
                    # reboot while following: boot_epoch is now stale, so every
                    # further line would be mis-stamped. End the stream and let
                    # the supervisor reconnect and re-establish it.
                    log.info("%s: uptime reset mid-follow — reboot, reconnecting", address)
                    return
                prev_up = up
                if up > seen:
                    seen = up
                    got_any = True
                    yield line
        if not got_any:
            # tail produced nothing new at all: likely a dead session rather
            # than a rotation — bail out and let the supervisor reconnect
            return
        log.info("%s: recorder tail went idle (rotation?) — reopening", address)


# Slack on the uptime ceiling below. It absorbs host/device clock drift and
# the lag between reading /proc/uptime and reading a line; it only has to stay
# small next to the distance between boots, which is hours.
_STALE_SLACK = 300.0


def _is_stale_boot(uptime: float, boot_epoch: float | None) -> bool:
    """True if this line belongs to a boot older than the current one.

    `tail -f -n 50` replays the last 50 lines before it starts following, and
    rec.log is append-only across reboots (§7), so on the first follow after a
    reboot that replay straddles the boot boundary. The backfill above is
    filtered by _current_boot_lines(); the replay never was, which is the whole
    bug it was written to prevent, leaking in through the other door:

      - the old boot's uptimes are huge, so they clear `up > seen`, get yielded,
        and are stamped with the NEW boot_epoch — points land hours in the
        future (the second failure mode in _current_boot_lines' docstring);
      - `seen` and the supervisor's last_uptime cursor jump to the old boot's
        maximum;
      - the first genuine line of this boot is then a decrease, so the
        reboot-mid-follow guard fires and ends the session. On reconnect the
        boot is no longer new, so the supervisor's reset does not run, the
        poisoned cursor survives, and the recorder tier stays dark for good.

    Observed 2026-08-12: four sticks that rebooted went silent for ~15 h and
    wrote 4,343 future-stamped points between them.

    The discriminator is that uptime cannot exceed the age of the current boot.
    """
    if not boot_epoch:
        return False
    return uptime > (time.time() - boot_epoch) + _STALE_SLACK


def _current_boot_lines(lines: list[str]) -> list[str]:
    """Return only the newest boot's segment of a rec.log dump.

    rec.sh is append-only across reboots (§7) and writes its header only when
    the file is absent, so one rec.log routinely holds several boots with no
    marker between them. Uptime is monotonic only WITHIN a boot; a decrease is
    a boot boundary.

    Both failure modes this prevents are real, seen on D-005-02408:
      - the dedup cursor is pinned to the previous boot's max uptime (9064),
        so every line of the new boot (302..805) fails `up > seen` and is
        dropped silently — the recorder tier goes permanently dark;
      - lines that DO replay are stamped with the current boot_epoch, putting
        points hours into the future (§1.11 wants a truthful wall clock).
    """
    start = 0
    prev = None
    for i, ln in enumerate(lines):
        up = _uptime_of(ln)
        if up is None:
            continue
        if prev is not None and up < prev:
            start = i
        prev = up
    return lines[start:]


def _top_uptime_of(line: str) -> float | None:
    """Uptime of a rec.sh v2 `#top` line, or None if it is not one.

    Kept separate from _uptime_of so boot-boundary detection stays driven by
    the 1 Hz lines alone — _current_boot_lines is load-bearing and tested.
    """
    if not line.startswith(TOP_PREFIX):
        return None
    parts = line.split("|", 2)
    if len(parts) < 2:
        return None
    try:
        return float(parts[1])
    except ValueError:
        return None


def _uptime_of(line: str) -> float | None:
    if not line or line.startswith("#"):
        return None
    head = line.split("|", 1)[0]
    try:
        return float(head)
    except ValueError:
        return None
