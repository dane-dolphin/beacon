from __future__ import annotations

import logging
import re
import time

from .adb import Adb
from .registry import Registry

log = logging.getLogger(__name__)

# §1.7 — log buffer remediation. Must be applied to EVERY device and
# VERIFIED rather than assumed. Whether persist.logd.size survives a reboot
# is still unconfirmed; verify_after_reboot() closes that gap.


async def apply_log_remediation(adb: Adb, address: str) -> None:
    await adb.shell(address, "setprop", "persist.logd.size", "4M")
    await adb.shell(address, "setprop", "persist.logd.filter", '""')
    await adb.shell(address, "logcat", "-b", "all", "-G", "4M")
    await adb.shell(address, "logd", "--reinit")


async def verify_log_remediation(adb: Adb, address: str) -> dict:
    """Check buffer sizes and chatty pruning. Returns a report dict."""
    out = await adb.shell(address, "logcat", "-g")
    # e.g. "main: ring buffer is 4 MiB (...consumed), max entry is 5120 B..."
    sizes = {}
    for line in out.splitlines():
        m = re.match(r"^(\w+):\s+ring buffer is\s+([\d.]+)\s*([KMG])i?B", line)
        if m:
            mult = {"K": 1 / 1024, "M": 1, "G": 1024}[m.group(3)]
            sizes[m.group(1)] = float(m.group(2)) * mult
    all_4m = bool(sizes) and all(v >= 4.0 for v in sizes.values())

    # actual chatty activity = 'chatty ... expire N lines' entries in the log
    # itself (§1.7); the --statistics header always contains 'Pruned' text.
    expire = await adb.shell(
        address, "logcat -d -t 5000 2>/dev/null | grep -c 'chatty.*expire'",
        timeout=30.0)
    try:
        chatty_active = int(expire.strip()) > 0
    except ValueError:
        chatty_active = False

    persist_size = (await adb.shell(address, "getprop", "persist.logd.size")).strip()

    return {
        "buffer_sizes_mib": sizes,
        "all_buffers_4mib": all_4m,
        "persist_logd_size": persist_size,
        "chatty_suspected": chatty_active,
    }


async def onboard(adb: Adb, address: str, serial: str, registry: Registry) -> dict:
    """Apply + verify remediation, record state. Safe to run repeatedly."""
    await apply_log_remediation(adb, address)
    registry.mark_onboarding(serial, "logd_applied_at")
    report = await verify_log_remediation(adb, address)
    if report["all_buffers_4mib"]:
        registry.mark_onboarding(serial, "logd_verified_at")
    else:
        log.warning("%s: log buffers NOT at 4MiB after remediation: %s",
                    serial, report["buffer_sizes_mib"])
    return report


async def verify_after_reboot(adb: Adb, address: str, serial: str, registry: Registry) -> bool:
    """Called by the supervisor after a detected reboot: did persist.logd.size stick?"""
    report = await verify_log_remediation(adb, address)
    if report["all_buffers_4mib"]:
        registry.mark_onboarding(serial, "logd_reboot_checked")
        log.info("%s: persist.logd.size CONFIRMED across reboot", serial)
        return True
    log.warning("%s: log buffers reverted after reboot (%s) — re-applying",
                serial, report["buffer_sizes_mib"])
    await apply_log_remediation(adb, address)
    return False


# --- probe: report which assumed commands actually work on a device (M0.5) ---

PROBE_CHECKS = [
    ("root", ("id",), "uid=0(root)"),
    ("serialno", ("getprop", "ro.serialno"), None),
    ("mali_debugfs", ("cat", "/sys/kernel/debug/mali0/gpu_memory"), "mali0"),
    ("meminfo", ("cat", "/proc/meminfo"), "MemAvailable"),
    ("thermal_soc", ("cat", "/sys/class/thermal/thermal_zone0/temp"), None),
    ("thermal_ddr", ("cat", "/sys/class/thermal/thermal_zone1/temp"), None),
    ("uptime", ("cat", "/proc/uptime"), None),
    ("setsid", ("which", "setsid"), "setsid"),
    ("toybox", ("which", "toybox"), "toybox"),
    ("data_local_tmp", ("touch", "/data/local/tmp/.beacon_probe"), None),
    ("app_pid", ("pidof", "com.dolphin_us.dolphinstore"), None),
    ("dumpsys_sf", ("dumpsys", "SurfaceFlinger", "--list"), None),
    ("boot_reason", ("getprop", "sys.boot.reason"), None),
    ("app_file_log", ("ls", "/sdcard/Android/data/com.dolphin_us.dolphinstore/files/Logs/"), None),
]


async def probe(adb: Adb, address: str) -> dict[str, dict]:
    """Run every assumed command; report works/fails with a sample of output."""
    results = {}
    for name, cmd, must_contain in PROBE_CHECKS:
        t0 = time.monotonic()
        try:
            out = await adb.shell(address, *cmd, timeout=15.0)
            ok = bool(out.strip()) if must_contain is None else (must_contain in out)
            # commands like touch legitimately return empty
            if not out.strip() and must_contain is None and name == "data_local_tmp":
                ok = True
            results[name] = {
                "ok": ok,
                "sample": out.strip().splitlines()[0][:120] if out.strip() else "",
                "ms": round((time.monotonic() - t0) * 1000),
            }
        except Exception as e:  # timeout, adb gone
            results[name] = {"ok": False, "sample": f"error: {e}", "ms": -1}
    return results
