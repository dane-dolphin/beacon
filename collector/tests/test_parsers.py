import time
from datetime import datetime, timezone
from pathlib import Path

from beacon_collector.parsers import dmesg
from beacon_collector.parsers.logcat import attach_year, parse_logcat_line
from beacon_collector.parsers.reassemble import Reassembler
from beacon_collector.parsers.recorder_line import (normalize_proc,
                                                    parse_recorder_line,
                                                    parse_top_line)
from beacon_collector.parsers import rn_fields
from beacon_collector import recorder

FIX = Path(__file__).parent / "fixtures"


# ---- recorder line (§1.12 measured format) -----------------------------------

def test_recorder_line_measured_sample():
    lines = (FIX / "rec_sample.txt").read_text().splitlines()
    assert parse_recorder_line(lines[0]) is None  # header comment

    s = parse_recorder_line(lines[1])
    assert s.uptime == 3208.85
    assert s.gpu_total_pages == 70597
    assert s.app_pid == 1689
    assert s.app_gpu_pages == 68672
    assert s.mem_available_kb == 2312732
    assert s.cma_free_kb == 580684
    assert s.temp_soc_milli == 76200
    assert s.temp_ddr_milli == 78600
    assert s.loadavg == (6.16, 5.98, 5.77)
    assert len(s.cpu) == 4
    assert s.cpu[0] == (588450, 204927, 402869)
    assert s.net == [(9820117, 5034536)]  # legacy 2-field chunk

    s2 = parse_recorder_line(lines[2])
    assert s2.net == [("wlan0", 9821000, 5035000)]  # v1 3-field chunk


# ---- rec.sh v2 #top line (per-process CPU/RAM) --------------------------------

def test_top_line_parses_all_processes():
    lines = (FIX / "rec_sample.txt").read_text().splitlines()
    t = parse_top_line(lines[-1])
    assert t.uptime == 3211.05
    assert t.mem_total_kb == 3840484
    assert t.clk_tck == 100
    assert len(t.procs) == 5

    app = t.procs[0]
    assert app.name == "com.dolphin_us.dolphinstore"
    assert (app.pid, app.rss_kb, app.pss_kb) == (1689, 719872, 703112)
    assert (app.utime_ticks, app.stime_ticks) == (58900, 14200)
    # rec.sh writes pss=0 when smaps_rollup is unreadable; that is "unavailable",
    # not "zero bytes", so it must not reach VM as a real measurement
    assert t.procs[3].pss_kb is None
    # a leading path is stripped on the device, and again here for safety
    assert t.procs[4].name == "surfaceflinger"


def test_top_line_renderer_digits_collapse_for_cardinality():
    # §5.1: :sandboxed_processN must not mint a series per renderer
    lines = (FIX / "rec_sample.txt").read_text().splitlines()
    t = parse_top_line(lines[-1])
    assert t.procs[1].name == "com.dolphin_us.dolphinstore:sandboxed_processN"

    assert normalize_proc("com.foo:sandboxed_process12") == "com.foo:sandboxed_processN"
    assert normalize_proc("com.foo:webview") == "com.foo:webview"
    # digits that are NOT a renderer counter carry meaning and stay
    assert normalize_proc("webview_zygote32") == "webview_zygote32"
    assert normalize_proc("/system/bin/surfaceflinger") == "surfaceflinger"
    # only an ABSOLUTE path is stripped: kernel threads are named kworker/0:1,
    # and a blind split on "/" would label them "0:N" — the identity is lost.
    # The remaining slash is rewritten by the charset rule, which is fine.
    assert normalize_proc("kworker/0:1") == "kworker_0:N"
    assert len(normalize_proc("x" * 200)) == 64


def test_v1_parser_untouched_by_v2_lines():
    """Existing captures in var/spool must keep parsing: the 1 Hz format did
    not change, and a #top line is simply not a sample."""
    lines = (FIX / "rec_sample.txt").read_text().splitlines()
    assert parse_top_line(lines[1]) is None          # 1 Hz line is not a top line
    assert parse_recorder_line(lines[-1]) is None    # top line is not a sample
    assert parse_recorder_line(lines[1]).uptime == 3208.85


def test_top_line_survives_a_malformed_process_chunk():
    line = "#top|10.0|100|100|good,1,2,3,4,5;truncated,9,9;other,2,3,4,5,6;"
    t = parse_top_line(line)
    assert [p.name for p in t.procs] == ["good", "other"]


def test_top_uptime_is_yielded_on_its_own_cursor():
    # a #top line must survive the 1 Hz dedup, which keys on uptime
    assert recorder._top_uptime_of("#top|123.4|1|100|a,1,2,3,4,5;") == 123.4
    assert recorder._top_uptime_of("123.4|1|2|3|4|5|6|7|1 1 1|1,2,3;|a,1,2;") is None
    assert recorder._uptime_of("#top|123.4|1|100|a,1,2,3,4,5;") is None


def test_boot_boundary_ignores_top_lines_but_keeps_them():
    """#top lines must not drive boot detection (their uptime is a coarse
    subset), but they must survive inside the returned segment."""
    dump = [
        "#beacon-rec v2 pkg=x",
        "9000.0|1|2|3|4|5|6|7|1 1 1|1,2,3;|a,1,2;",     # previous boot
        "#top|9000.0|100|100|a,1,2,3,4,5;",             # previous boot
        "10.0|1|2|3|4|5|6|7|1 1 1|1,2,3;|a,1,2;",       # <- reboot here
        "#top|11.0|100|100|a,1,2,3,4,5;",
        "12.0|1|2|3|4|5|6|7|1 1 1|1,2,3;|a,1,2;",
    ]
    seg = recorder._current_boot_lines(dump)
    assert seg == dump[3:]
    assert any(l.startswith("#top|") for l in seg)


def test_recorder_wall_ts_uses_boot_epoch():
    s = parse_recorder_line(
        "100.5|1|2|3|4|5|6|7|1.0 1.0 1.0|1,2,3;|a,1,2;")
    assert s.wall_ts(boot_epoch=1_700_000_000.0) == 1_700_000_100.5


# ---- logcat (§3.1) + year attachment (§1.11) ----------------------------------

def test_logcat_regex_fields():
    raw = "07-27 12:47:32.929  1689  2034 I ReactNativeJS: 'Scheduler:: playNextAd'"
    p = parse_logcat_line(raw, time.time())
    assert (p.pid, p.tid, p.level, p.tag) == (1689, 2034, "I", "ReactNativeJS")
    assert p.message.startswith("'Scheduler")


def test_year_attach_normal():
    recv = datetime(2026, 7, 27, 12, 47, 40, tzinfo=timezone.utc).timestamp()
    ts = attach_year("07-27 12:47:32.929", recv)
    assert datetime.fromtimestamp(ts, tz=timezone.utc).year == 2026


def test_year_attach_dec_jan_rollover():
    # device logged Dec 31, collector received it Jan 1 of the NEXT year
    recv = datetime(2027, 1, 1, 0, 0, 5, tzinfo=timezone.utc).timestamp()
    ts = attach_year("12-31 23:59:58.000", recv)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert (dt.year, dt.month, dt.day) == (2026, 12, 31)


def test_no_yearless_timestamp_ever_leaves_parser():
    # §1.11: device_ts must be a full epoch — sanity: within days of receive time
    recv = time.time()
    p = parse_logcat_line("07-27 12:47:32.929  1  2 I X: y", recv)
    assert isinstance(p.device_ts, float)
    assert abs(p.device_ts - recv) < 366 * 24 * 3600


def test_unparsable_returns_none():
    assert parse_logcat_line("--------- beginning of main", time.time()) is None


# ---- reassembly (§3.2) ---------------------------------------------------------

def test_reassemble_creative_change_block():
    recv = datetime(2026, 7, 27, 12, 47, 40, tzinfo=timezone.utc).timestamp()
    lines = (FIX / "logcat_sample.txt").read_text().splitlines()
    r = Reassembler()
    events = []
    for raw in lines:
        p = parse_logcat_line(raw, recv)
        if p:
            events.extend(r.feed(p))
    events.extend(r.flush())

    # the 12-line creative-change block is ONE logical event,
    # despite straddling the .929 -> .930 millisecond boundary
    big = max(events, key=lambda e: e.n_lines)
    assert big.n_lines == 12
    assert "creativeId" in big.text and "venueCode" in big.text
    # and the three repeated socket errors are three separate events
    socket_events = [e for e in events if "Unix domain socket" in e.text]
    assert len(socket_events) == 3


# ---- rn field derivation (§1.9/§5.2) -------------------------------------------

def test_extract_fields_from_block():
    text = (FIX / "logcat_sample.txt").read_text()
    f = rn_fields.extract_fields(text)
    assert f["vc"] == "183"
    assert f["creative_id"] == "azdd-PRODUCT-1778274790998"
    assert f["campaign_id"] == "PRODUCT-1776822280013"
    assert f["duration_secs"] == "5"
    assert f["mime"] == "weblink"
    assert f["exchange_type"] == "OW_DL"
    assert f["d_id"] == "DD-H33CBP"
    assert f["installation_id"] == "63718089"
    assert f["hdmi_connected"] == "0"
    assert f["webview_swap"] == "WEB_VIEW_2"


def test_creative_tracker_intervals():
    t = rn_fields.CreativeTracker("S1")
    assert t.observe(100.0, {"creative_id": "A", "vc": "183"}) is None
    assert t.observe(105.0, {"creative_id": "A"}) is None      # same creative
    closed = t.observe(110.0, {"creative_id": "B"})
    assert closed.creative_id == "A"
    assert closed.start_ts == 100.0 and closed.end_ts == 110.0
    assert closed.app_version == "183"
    assert t.current.creative_id == "B"


# ---- dmesg (§1.5) ---------------------------------------------------------------

def test_dmesg_fault_signatures():
    lines = (FIX / "dmesg_sample.txt").read_text().splitlines()
    types = [dmesg.gpu_fault_type(l) for l in lines]
    assert types[0] is None                       # wlan line
    assert types[1] == "DATA_INVALID_FAULT"
    assert types[2] == "GPU_fault_other"
    assert types[3] == "Unhandled_Page_fault"
    assert types[4] == "Job_Hard_Stopped"
    assert types[5] == "Reset_complete"
    assert types[6] == "JOB_READ_FAULT"
    assert types[7] is None                       # audit line


def test_dmesg_monotonic_parse():
    mono, msg = dmesg.parse("[  502.604062] mali fe400000.bifrost: t6xx: GPU fault")
    assert mono == 502.604062
    assert msg.startswith("mali")


# ---- recorder boot boundaries (§7 append-only across reboots) -------------------

def test_current_boot_lines_drops_previous_boot():
    """rec.log persists across reboots with no marker, so a dump can hold
    several boots. Only the newest segment may be replayed: the older one
    belongs to a different boot_epoch. Regression — on D-005-02408 the stale
    segment pinned the `up > seen` cursor at 9064 and the whole new boot
    (302..805) was dropped, silently blacking out the 1 Hz tier."""
    dump = [
        "#beacon-rec v1 pkg=com.x",
        "9062.10|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;",
        "9064.23|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;",
        "302.97|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;",   # <- reboot here
        "304.05|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;",
    ]
    seg = recorder._current_boot_lines(dump)
    ups = [recorder._uptime_of(l) for l in seg]
    assert ups == [302.97, 304.05]


def test_current_boot_lines_keeps_single_boot_intact():
    dump = [
        "#beacon-rec v1 pkg=com.x",
        "10.00|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;",
        "11.10|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;",
    ]
    assert recorder._current_boot_lines(dump) == dump


def test_current_boot_lines_uses_last_boundary_of_three():
    dump = [f"{u}|1|2|3|4|5|6|7|1 1 1|1,1,1;|wlan0,1,2;"
            for u in (500.0, 501.0, 20.0, 21.0, 5.0, 6.0)]
    ups = [recorder._uptime_of(l) for l in recorder._current_boot_lines(dump)]
    assert ups == [5.0, 6.0]
