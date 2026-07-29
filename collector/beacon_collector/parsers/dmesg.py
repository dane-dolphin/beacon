from __future__ import annotations

import re

# §1.5 — GPU fault signature set, captured live on hardware:
#   [502.604062] mali fe400000.bifrost: error detected from slot 0,
#                job status 0x00000058 (DATA_INVALID_FAULT)
# Fault *types* become the low-cardinality `type` label on
# stick_gpu_faults_total; the raw text goes to Loki/Parquet, not VM (§5).

_SIGNATURES = [
    ("JOB_READ_FAULT", re.compile(r"JOB_READ_FAULT")),
    ("DATA_INVALID_FAULT", re.compile(r"DATA_INVALID_FAULT")),
    ("Unhandled_Page_fault", re.compile(r"Unhandled Page fault", re.I)),
    ("Job_Hard_Stopped", re.compile(r"Job Hard-Stopped")),
    ("Reset_complete", re.compile(r"Reset complete")),
    ("next_index_above_max", re.compile(r"next index above max")),
    # generic catch-all LAST: any other mali GPU fault line
    ("GPU_fault_other", re.compile(r"mali .*GPU fault", re.I)),
]

TS = re.compile(r"^\[\s*(\d+\.\d+)\]\s*(.*)$")


def gpu_fault_type(line: str) -> str | None:
    """Classify a dmesg line into a fault-type label, or None."""
    if "mali" not in line and "Mali" not in line:
        return None
    for name, rx in _SIGNATURES:
        if rx.search(line):
            return name
    return None


def parse(line: str) -> tuple[float, str] | None:
    """Split '[  502.604062] msg' -> (monotonic_seconds, msg).
    Monotonic only — callers MUST add boot_epoch for wall time (§1.11).
    logcat's kernel buffer carries bogus pre-NTP wall-clock; never use it."""
    m = TS.match(line)
    if not m:
        return None
    return float(m.group(1)), m.group(2)
