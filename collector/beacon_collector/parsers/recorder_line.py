from __future__ import annotations

import re
from dataclasses import dataclass, field

# Parses rec.sh records (device/rec.sh). Reference 1 Hz sample from §1.12:
# 3208.85|70597|1689|68672|2312732|580684|76200|78600|6.16 5.98 5.77|588450,204927,402869;...|9820117,5034536;
#
# rec.sh v2 adds a second, low-frequency line type handled by parse_top_line:
# #top|uptime|MemTotal_kB|clk_tck|name,pid,rss_kb,pss_kb,utime,stime;...
#
# The 1 Hz format is byte-identical between v1 and v2, so parse_recorder_line
# is untouched and every existing capture in var/spool keeps parsing.

TOP_PREFIX = "#top|"


@dataclass
class RecorderSample:
    uptime: float                 # device seconds since boot — the self-timestamp
    gpu_total_pages: int | None
    app_pid: int | None
    app_gpu_pages: int | None
    mem_available_kb: int | None
    cma_free_kb: int | None
    temp_soc_milli: int | None
    temp_ddr_milli: int | None
    loadavg: tuple[float, float, float] | None
    # per-core (user, system, idle) jiffies
    cpu: list[tuple[int, int, int]] = field(default_factory=list)
    # per-interface (iface, rx_bytes, tx_bytes), or (rx_bytes, tx_bytes) legacy
    net: list[tuple] = field(default_factory=list)

    def wall_ts(self, boot_epoch: float) -> float:
        """Convert the device-monotonic stamp to wall clock (§1.11)."""
        return boot_epoch + self.uptime


@dataclass
class ProcSample:
    """One process from a #top line. Names are already normalized (§5.1)."""
    name: str
    pid: int
    rss_kb: int
    pss_kb: int | None            # None = smaps_rollup unavailable on this device
    utime_ticks: int
    stime_ticks: int


@dataclass
class TopSample:
    uptime: float                 # device seconds since boot — the self-timestamp
    mem_total_kb: int | None
    clk_tck: int                  # from getconf on the device; never assumed
    procs: list[ProcSample] = field(default_factory=list)

    def wall_ts(self, boot_epoch: float) -> float:
        return boot_epoch + self.uptime


_TRAILING_DIGITS = re.compile(r"\d+$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._:@-]")
MAX_PROC_NAME = 64


def normalize_proc(name: str) -> str:
    """Collapse a process name to a BOUNDED set of label values (§5.1).

    Every distinct value here becomes a permanent VictoriaMetrics series, and
    dead series still cost index and query time — this is the same cardinality
    trap that produced 513 `stick_errors_total{sig}` series from one device in
    one hour. Chrome/WebView spawn `:sandboxed_process0`, `:sandboxed_process12`
    and so on, which would mint a new series per renderer, so digits trailing
    the LAST colon segment collapse to N. Digits elsewhere are meaningful
    (`webview_zygote32` is the 32-bit zygote) and are left alone.
    """
    name = name.strip()
    if name.startswith("/"):
        name = name.rsplit("/", 1)[-1]        # /system/bin/foo -> foo
    # NOT an unconditional split on "/": kernel threads are named kworker/0:1,
    # and stripping to the last segment would turn that into "0:1".
    if ":" in name:
        head, _, tail = name.rpartition(":")
        name = f"{head}:{_TRAILING_DIGITS.sub('N', tail)}"
    name = _UNSAFE.sub("_", name)
    return name[:MAX_PROC_NAME]


def parse_top_line(line: str) -> TopSample | None:
    """Parse a rec.sh v2 `#top` line. Returns None for anything else, so it is
    safe to call on every recorder line."""
    if not line.startswith(TOP_PREFIX):
        return None
    parts = line.split("|")
    if len(parts) != 5:
        return None
    try:
        uptime = float(parts[1])
    except ValueError:
        return None

    clk = _int(parts[3]) or 100          # rec.sh defaults to 100 when getconf fails
    procs: list[ProcSample] = []
    for chunk in parts[4].split(";"):
        if not chunk:
            continue
        vals = chunk.split(",")
        if len(vals) != 6:
            continue
        pid, rss, pss, ut, st = (_int(v) for v in vals[1:])
        if None in (pid, rss, ut, st) or not vals[0]:
            continue
        procs.append(ProcSample(
            name=normalize_proc(vals[0]),
            pid=pid,
            rss_kb=rss,
            # rec.sh writes 0 for "unavailable"; a real process is never 0 kB
            pss_kb=pss if pss else None,
            utime_ticks=ut,
            stime_ticks=st,
        ))
    return TopSample(uptime=uptime, mem_total_kb=_int(parts[2]),
                     clk_tck=clk, procs=procs)


def _int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def parse_recorder_line(line: str) -> RecorderSample | None:
    if not line or line.startswith("#"):
        return None
    parts = line.split("|")
    if len(parts) != 11:
        return None
    try:
        uptime = float(parts[0])
    except ValueError:
        return None

    loadavg = None
    la = parts[8].split()
    if len(la) == 3:
        try:
            loadavg = (float(la[0]), float(la[1]), float(la[2]))
        except ValueError:
            pass

    cpu = []
    for chunk in parts[9].split(";"):
        if not chunk:
            continue
        vals = chunk.split(",")
        if len(vals) == 3:
            u, s, i = _int(vals[0]), _int(vals[1]), _int(vals[2])
            if None not in (u, s, i):
                cpu.append((u, s, i))

    net = []
    for chunk in parts[10].split(";"):
        if not chunk:
            continue
        vals = chunk.split(",")
        if len(vals) == 3:  # iface,rx,tx (rec.sh v1)
            rx, tx = _int(vals[1]), _int(vals[2])
            if None not in (rx, tx):
                net.append((vals[0], rx, tx))
        elif len(vals) == 2:  # rx,tx (§1.12 measured sample)
            rx, tx = _int(vals[0]), _int(vals[1])
            if None not in (rx, tx):
                net.append((rx, tx))

    app_pid = _int(parts[2])
    return RecorderSample(
        uptime=uptime,
        gpu_total_pages=_int(parts[1]),
        app_pid=app_pid if app_pid else None,   # rec.sh writes 0 when app absent
        app_gpu_pages=_int(parts[3]) or None,
        mem_available_kb=_int(parts[4]),
        cma_free_kb=_int(parts[5]),
        temp_soc_milli=_int(parts[6]),
        temp_ddr_milli=_int(parts[7]),
        loadavg=loadavg,
        cpu=cpu,
        net=net,
    )
