from __future__ import annotations

from dataclasses import dataclass, field

# Parses rec.sh v1 records (device/rec.sh). Reference sample from §1.12:
# 3208.85|70597|1689|68672|2312732|580684|76200|78600|6.16 5.98 5.77|588450,204927,402869;...|9820117,5034536;


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
