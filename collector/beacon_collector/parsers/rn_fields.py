from __future__ import annotations

import re
from dataclasses import dataclass

# §1.9 — the app already emits rich telemetry in ReactNativeJS output.
# These extractors derive tags for free: app_version (vc), creative
# intervals, HDMI ground truth, identity cross-refs. Derived, not declared
# (§5.2). creative_id is an EVENT ROW, never a metric label (§5.1).

_FIELD_RES = {
    "vc": re.compile(r"\bvc['\"]?\s*[:=]\s*['\"]?(\d+)"),
    "version_code": re.compile(r"\bversionCode['\"]?\s*[:=]\s*['\"]?(\d+)"),
    "creative_id": re.compile(r"\bcreativeId['\"]?\s*[:=]\s*['\"]?([\w.-]+)"),
    "content_id": re.compile(r"\bcontentId['\"]?\s*[:=]\s*['\"]?([\w.-]+)"),
    "campaign_id": re.compile(r"\bcampaignId['\"]?\s*[:=]\s*['\"]?([\w.-]+)"),
    "url": re.compile(r"\burl['\"]?\s*[:=]\s*['\"]?(https?://\S+?)['\"],?\s*$", re.M),
    "duration_secs": re.compile(r"\bdurationSecs['\"]?\s*[:=]\s*['\"]?(\d+)"),
    "mime": re.compile(r"\bmime['\"]?\s*[:=]\s*['\"]?([\w/-]+)"),
    "exchange_type": re.compile(r"\bexchangeType['\"]?\s*[:=]\s*['\"]?([\w-]+)"),
    "d_id": re.compile(r"\bdId['\"]?\s*[:=]\s*['\"]?([\w-]+)"),
    "venue_code": re.compile(r"\bvenueCode['\"]?\s*[:=]\s*['\"]?([\w-]+)"),
    "installation_id": re.compile(r"\binstallationId['\"]?\s*[:=]\s*['\"]?([\w-]+)"),
}

_HDMI = re.compile(r"getHdmiStatus'?,?\s*(true|false)", re.I)
_WEBVIEW_SWAP = re.compile(r"Setting Active view as\s+(WEB_VIEW_\d)")


def extract_fields(text: str) -> dict[str, str]:
    out = {}
    for name, rx in _FIELD_RES.items():
        m = rx.search(text)
        if m:
            out[name] = m.group(1)
    if "vc" not in out and "version_code" in out:
        out["vc"] = out["version_code"]
    m = _HDMI.search(text)
    if m:
        out["hdmi_connected"] = "1" if m.group(1).lower() == "true" else "0"
    m = _WEBVIEW_SWAP.search(text)
    if m:
        out["webview_swap"] = m.group(1)
    return out


@dataclass
class CreativeInterval:
    """One §5.1 event row: unlimited cardinality, costs nothing in Parquet."""
    serial: str
    start_ts: float
    end_ts: float | None
    creative_id: str
    campaign_id: str | None
    url: str | None
    duration_secs: int | None
    mime: str | None
    exchange_type: str | None
    app_version: str | None


class CreativeTracker:
    """Turns per-line creative sightings into intervals: a change closes the
    previous interval and opens a new one. Also tracks the latest derived
    app_version so every interval carries its build (§5.2)."""

    def __init__(self, serial: str):
        self.serial = serial
        self.current: CreativeInterval | None = None
        self.app_version: str | None = None

    def observe(self, ts: float, fields: dict[str, str]) -> CreativeInterval | None:
        """Returns the CLOSED interval when a creative change is seen."""
        if "vc" in fields:
            self.app_version = fields["vc"]
        cid = fields.get("creative_id")
        if not cid:
            return None
        if self.current and self.current.creative_id == cid:
            return None
        closed = None
        if self.current:
            self.current.end_ts = ts
            closed = self.current
        self.current = CreativeInterval(
            serial=self.serial,
            start_ts=ts,
            end_ts=None,
            creative_id=cid,
            campaign_id=fields.get("campaign_id"),
            url=fields.get("url"),
            duration_secs=int(fields["duration_secs"]) if fields.get("duration_secs") else None,
            mime=fields.get("mime"),
            exchange_type=fields.get("exchange_type"),
            app_version=self.app_version,
        )
        return closed
