from __future__ import annotations

import re
from dataclasses import dataclass

from ..parsers.reassemble import LogicalEvent

# §3.3/§3.4 — the four filtering tiers, with the non-negotiable safety
# rules: DENYLIST never allowlist (default is ship); denylisted tags are
# SAMPLED 1:100, not zeroed; dedup counts ship as metrics; the unparsed
# bucket always ships. Filtering only gates the interactive (Loki) tier —
# everything still reaches Parquet from the raw spool.

DENYLIST = {
    "ThermalService", "AbstractTask", "NetworkScheduler.Stats", "WakeLock",
    "GCM", "TaskProcessor", "DeviceStatusUpdateTask", "EsperRetryInterceptor",
    "EsperSharedDataContentProviderHelper", "MqttAndroidClientConnectionManager",
    "wifi@1.0-servic", "PackageWatchdog", "ExplicitHealthCheckController",
    "SystemServerTimingAsync", "CompatibilityChangeReporter",
    "ConnectivityService", "ExperimentPackageManage", "StrictMode",
}

SAMPLE_RATE = 100  # 1-in-100 keeps a denylisted tag alive (§3.4 rule 3)

_NORMALIZE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xHEX"),
    (re.compile(r"\b\d+\b"), "N"),
]


def signature(tag: str, message: str) -> str:
    """Normalized error signature for dedup: digits/hex collapsed so
    'expire 77 lines' and 'expire 81 lines' count as one signature."""
    s = message
    for rx, repl in _NORMALIZE:
        s = rx.sub(repl, s)
    return f"{tag}|{s[:200]}"


@dataclass
class Decision:
    ship: bool                      # send to Loki?
    reason: str                     # tier1..tier4 | sampled | unparsed
    dedup_sig: str | None = None    # set for E/F: caller ships exemplar + counter
    is_exemplar: bool = False


class TierFilter:
    """Stateful per-device classification of logical events."""

    def __init__(self):
        self._sig_counts: dict[str, int] = {}
        self._tag_skips: dict[str, int] = {}

    def classify(self, ev: LogicalEvent) -> Decision:
        level, tag = ev.first.level, ev.first.tag

        if level in ("E", "F"):
            sig = signature(tag, ev.first.message)
            n = self._sig_counts.get(sig, 0) + 1
            self._sig_counts[sig] = n
            # 3 exemplars per signature ship verbatim; the rest ride the counter
            return Decision(ship=n <= 3, reason="tier1", dedup_sig=sig, is_exemplar=n <= 3)

        if level == "W":
            return Decision(ship=True, reason="tier2")

        if tag == "ReactNativeJS" and ev.n_lines > 1:
            # multi-line app JSON: the caller ships ONE structured row, not the text
            return Decision(ship=False, reason="tier3")

        if tag in DENYLIST:
            n = self._tag_skips.get(tag, 0) + 1
            self._tag_skips[tag] = n
            if n % SAMPLE_RATE == 0:
                return Decision(ship=True, reason="sampled")
            return Decision(ship=False, reason="tier4")

        # §3.4 rule 1: default is SHIP. New tags after a build change are
        # exactly what this rig exists to see.
        return Decision(ship=True, reason="default")

    def sig_count(self, sig: str) -> int:
        return self._sig_counts.get(sig, 0)
