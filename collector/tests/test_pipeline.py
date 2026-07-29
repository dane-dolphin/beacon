import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from beacon_collector.parsers.logcat import parse_logcat_line
from beacon_collector.parsers.reassemble import Reassembler
from beacon_collector.parsers.recorder_line import parse_recorder_line
from beacon_collector.pipeline.metrics import line, sample_to_lines
from beacon_collector.pipeline.spool import RawSpool
from beacon_collector.pipeline.tiers import DENYLIST, TierFilter, signature

FIX = Path(__file__).parent / "fixtures"


# ---- tier filter (§3.3/§3.4) ---------------------------------------------------

def _one(raw):
    r = Reassembler()
    p = parse_logcat_line(raw, time.time())
    r.feed(p)
    return r.flush()[0]


def test_tier1_errors_dedup_three_exemplars():
    tf = TierFilter()
    decisions = [
        tf.classify(_one(f"07-27 12:47:3{i}.000  2533  2533 E ocessService0: "
                         f"failed to create Unix domain socket: attempt {i}"))
        for i in range(5)
    ]
    # digits are normalized out of the signature, so all 5 share one signature
    assert len({d.dedup_sig for d in decisions}) == 1
    assert [d.ship for d in decisions] == [True, True, True, False, False]


def test_tier2_warnings_ship_verbatim():
    tf = TierFilter()
    d = tf.classify(_one("07-27 12:00:00.000  1 1 W ActivityManager: slow"))
    assert d.ship and d.reason == "tier2"


def test_tier4_denylist_samples_1_in_100():
    tf = TierFilter()
    ships = sum(
        tf.classify(_one(f"07-27 12:00:{i % 60:02d}.000  1 1 I ThermalService: CPU temperatures: [{i}]")).ship
        for i in range(200)
    )
    assert ships == 2  # §3.4 rule 3: sample, never zero
    assert "ThermalService" in DENYLIST


def test_default_is_ship_not_allowlist():
    tf = TierFilter()
    d = tf.classify(_one("07-27 12:00:00.000  1 1 I BrandNewTagAfterBuildChange: hello"))
    assert d.ship  # §3.4 rule 1


def test_signature_normalizes_numbers_and_hex():
    a = signature("mali", "GPU fault 0x58 from job slot 0")
    b = signature("mali", "GPU fault 0xa3 from job slot 2")
    assert a == b


# ---- metrics emission (§5) -----------------------------------------------------

def test_sample_to_lines_schema():
    raw = (FIX / "rec_sample.txt").read_text().splitlines()[1]
    s = parse_recorder_line(raw)
    boot_epoch = 1_785_000_000.0
    lines = sample_to_lines("D-005-02408", s, boot_epoch)
    text = "\n".join(lines)

    assert "stick_gpu_pages_total,serial=D-005-02408 value=70597" in text
    assert "stick_gpu_pages,proc=dplayer,serial=D-005-02408 value=68672" in text
    assert "stick_temp_celsius,serial=D-005-02408,zone=soc value=76.2" in text
    assert "stick_mem_available_bytes,serial=D-005-02408 value=2368237568" in text
    # every line ends with the same full ns timestamp derived from boot_epoch
    expect_ns = str(int((boot_epoch + s.uptime) * 1e9))
    assert all(l.endswith(expect_ns) for l in lines)
    # §5.1: creative_id never appears as a label
    assert "creative" not in text


def test_line_protocol_escaping():
    l = line("m", {"serial": "has space", "x": "a,b"}, 1, 1.0)
    assert r"has\ space" in l and r"a\,b" in l


# ---- raw spool ------------------------------------------------------------------

def test_rawspool_hour_partitioning(tmp_path):
    spool = RawSpool(tmp_path)
    ts = datetime(2026, 7, 27, 14, 30, 0, tzinfo=timezone.utc).timestamp()
    spool.append("D-005-02408", "logcat", "line one", ts=ts)
    spool.close()
    f = tmp_path / "D-005-02408" / "logcat" / "dt=2026-07-27" / "hour=14.jsonl"
    assert f.exists()
    rec = json.loads(f.read_text().strip())
    assert rec["ts"] == ts and rec["line"] == "line one"


# ---- logcat -T quoting (§1.7 reconnect backfill) --------------------------------

def test_logcat_since_is_quoted_for_the_device_shell():
    """adb joins argv with spaces and the DEVICE shell re-splits, so a bare
    'MM-DD HH:MM:SS.mmm' arrives as two tokens and logcat exits with
    "not in time format". That EOF tore down the session on every reconnect
    and spun the supervisor in a collect-nothing loop after the 22:00 reboot."""
    import asyncio
    from beacon_collector import streams

    seen = {}

    class FakeStream:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def lines(self, **kw):
            if False:
                yield ""

    class FakeAdb:
        def stream(self, address, *cmd):
            seen["cmd"] = cmd
            return FakeStream()

    async def drain(since):
        async for _ in streams.logcat_lines(FakeAdb(), "h:5555", since):
            pass

    asyncio.run(drain("07-28 23:30:00.000"))
    cmd = seen["cmd"]
    assert "-T" in cmd
    val = cmd[cmd.index("-T") + 1]
    assert val == "'07-28 23:30:00.000'", val
    # the whole timestamp must survive as ONE device-shell token
    assert val.startswith("'") and val.endswith("'")

    asyncio.run(drain(None))
    assert "-T" not in seen["cmd"]
