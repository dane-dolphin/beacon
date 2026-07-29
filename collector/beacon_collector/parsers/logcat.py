from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

# §3.1 — validated regex for `logcat -v threadtime`.
LOGCAT = re.compile(
    r'^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?):\s?(.*)$'
)


@dataclass
class LogcatLine:
    device_ts: float      # full epoch seconds — year attached, NEVER year-less (§1.11)
    ts_key: str           # raw 'MM-DD HH:MM:SS.mmm' string, reassembly grouping key
    pid: int
    tid: int
    level: str
    tag: str
    message: str
    raw: str


def attach_year(ts_noyear: str, receive_time: float) -> float:
    """'07-27 12:47:32.929' + collector receive-time -> full epoch.

    The year is chosen so the device timestamp lands closest to receive_time,
    which handles the Dec->Jan rollover in both directions. This is the §1.11
    rule: a year-less timestamp must never be stored (the bug that made the
    old pipeline's cleanup() wipe its whole table).
    """
    ryear = datetime.fromtimestamp(receive_time, tz=timezone.utc).year
    best = None
    for year in (ryear - 1, ryear, ryear + 1):
        try:
            dt = datetime.strptime(f"{year}-{ts_noyear}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap candidate year
        ts = dt.replace(tzinfo=timezone.utc).timestamp()
        if best is None or abs(ts - receive_time) < abs(best - receive_time):
            best = ts
    return best if best is not None else receive_time


def parse_logcat_line(raw: str, receive_time: float,
                      tz_offset_s: float = 0.0) -> LogcatLine | None:
    """Parse one threadtime line; None for unparsable (§3.4 rule 2: the
    caller must count and SHIP unparsed lines, not drop them).

    tz_offset_s: the device's UTC offset (`date +%z`), because logcat prints
    DEVICE-LOCAL time — e.g. CDT lines parsed as UTC would land 5 h in the
    past. Kernel-buffer lines can still carry the bogus pre-NTP wall clock
    '12-31 18:00:05' (§1.11); plausibility-clamp device_ts to receive_time
    when it is far off, since receive-time is the primary stamp anyway.
    """
    m = LOGCAT.match(raw)
    if not m:
        return None
    ts_noyear, pid, tid, level, tag, message = m.groups()
    device_ts = attach_year(ts_noyear, receive_time) - tz_offset_s
    if not (receive_time - 86400 <= device_ts <= receive_time + 300):
        device_ts = receive_time
    return LogcatLine(
        device_ts=device_ts,
        ts_key=ts_noyear,
        pid=int(pid),
        tid=int(tid),
        level=level,
        tag=tag.strip(),
        message=message,
        raw=raw,
    )
