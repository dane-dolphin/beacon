"""Regression tests for the follow-across-a-reboot bug (2026-08-12).

rec.log is append-only across reboots (§7) and `tail -f -n 50` replays the last
50 lines before it follows, so the first follow after a reboot straddles a boot
boundary. The backfill path was filtered by _current_boot_lines(); the replay
path was not. Four sticks lost ~15 h of metrics and wrote 4,343 future-stamped
points before this was caught.
"""

import asyncio
import time

from beacon_collector import recorder
from beacon_collector.recorder import _is_stale_boot


# ---- fakes -------------------------------------------------------------------

class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def lines(self, idle_timeout=None):
        for ln in self._lines:
            yield ln


class _FakeAdb:
    """Serves one `cat` dump and a fixed `tail -f` replay."""

    def __init__(self, dump_lines, tail_lines):
        self.dump = "\n".join(dump_lines)
        self.tail_lines = list(tail_lines)

    async def shell(self, address, *cmd, timeout=20.0):
        return self.dump

    def stream(self, address, *cmd):
        return _FakeStream(self.tail_lines)


def _sample(uptime):
    """A 1 Hz rec.sh line; only the leading uptime field matters here."""
    return f"{uptime}|1024|999|64|512000|8192|45000|46000|0.1 0.2 0.3|1,2,3;|wlan0,10,20;"


def _follow(adb, boot_epoch, last_uptime=0.0, cap=200):
    """Drain backfill_and_follow to a list. Sync so the suite needs no async
    pytest plugin — the project does not depend on one."""
    async def go():
        out = []
        gen = recorder.backfill_and_follow(adb, "1.2.3.4:5555", last_uptime, boot_epoch)
        async for line in gen:
            out.append(line)
            if len(out) >= cap:
                break
        await gen.aclose()
        return out
    return asyncio.run(go())


def _uptimes(lines):
    return [float(ln.split("|", 1)[0]) for ln in lines if not ln.startswith("#")]


# ---- the guard itself --------------------------------------------------------

def test_is_stale_boot_flags_previous_boot():
    # device has been up 65 s; a line claiming 86366 s predates this boot
    boot_epoch = time.time() - 65
    assert _is_stale_boot(86366.0, boot_epoch) is True
    assert _is_stale_boot(60.0, boot_epoch) is False


def test_is_stale_boot_tolerates_clock_drift():
    boot_epoch = time.time() - 65
    # inside the slack: not stale, however unflattering the clock
    assert _is_stale_boot(65 + recorder._STALE_SLACK - 10, boot_epoch) is False
    assert _is_stale_boot(65 + recorder._STALE_SLACK + 10, boot_epoch) is True


def test_is_stale_boot_is_inert_without_boot_epoch():
    # unknown boot_epoch keeps the old, unguarded behaviour rather than
    # silently dropping every line
    assert _is_stale_boot(86366.0, None) is False
    assert _is_stale_boot(86366.0, 0) is False


# ---- the regression ----------------------------------------------------------

def test_follow_after_reboot_ignores_previous_boot_replay():
    prev = [_sample(u) for u in range(86360, 86367)]   # yesterday's boot
    cur = [_sample(u) for u in range(60, 67)]          # this boot, 60 s in
    boot_epoch = time.time() - 66

    # rec.log holds both boots; tail -n 50 replays the end of the file, which
    # here spans the boundary exactly as it does on a small post-reboot file.
    adb = _FakeAdb(prev + cur, prev + cur)

    got = _follow(adb, boot_epoch)

    ups = _uptimes(got)
    assert ups, "the current boot's lines must still be ingested"
    # the actual bug: previous-boot lines reaching the ingest path, where they
    # are stamped with the NEW boot_epoch and land hours in the future
    assert max(ups) < 1000, f"previous-boot lines leaked into the stream: {ups}"
    assert set(ups) == set(range(60, 67))


def test_follow_after_reboot_does_not_end_the_session():
    """The poisoned cursor was permanent: the replay pushed prev_up to the old
    boot's max, the first real line read as a decrease, and the generator
    returned — tearing down the session (FIRST_COMPLETED) every cycle."""
    prev = [_sample(u) for u in range(86360, 86367)]
    cur = [_sample(u) for u in range(60, 67)]
    boot_epoch = time.time() - 66
    adb = _FakeAdb(prev + cur, prev + cur)

    got = _follow(adb, boot_epoch)

    # every current-boot line survives; nothing is lost to an early return
    assert len(_uptimes(got)) == 7


def test_genuine_reboot_mid_follow_still_ends_the_stream():
    """The guard must not swallow a real reboot: uptime resetting while we
    follow means boot_epoch is stale and every later line would be mis-stamped,
    so the generator must still end and let the supervisor re-establish."""
    boot_epoch = time.time() - 86400          # device up 24 h
    cur = [_sample(u) for u in range(86360, 86367)]
    after = [_sample(u) for u in (1, 2, 3)]   # reboot lands mid-follow
    adb = _FakeAdb(cur, cur + after)

    got = _follow(adb, boot_epoch)

    ups = _uptimes(got)
    assert 86366 in ups
    assert 1 not in ups and 2 not in ups, "post-reboot lines need a fresh boot_epoch"


def test_top_lines_from_previous_boot_are_dropped():
    """#top lines carry their own cursor, so they need the same guard."""
    boot_epoch = time.time() - 66
    stale_top = "#top|86366|2048000|100|app,999,1024,512,10,20;"
    cur_top = "#top|61|2048000|100|app,999,1024,512,10,20;"
    cur = [_sample(u) for u in range(60, 67)]
    adb = _FakeAdb(cur, [stale_top] + cur + [cur_top])

    got = _follow(adb, boot_epoch)

    assert stale_top not in got
    assert cur_top in got
