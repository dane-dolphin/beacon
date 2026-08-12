import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from beacon_collector.parsers.logcat import parse_logcat_line
from beacon_collector.parsers.reassemble import Reassembler
from beacon_collector.parsers.recorder_line import parse_recorder_line, parse_top_line
from beacon_collector.pipeline.metrics import line, sample_to_lines, top_to_lines
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


def test_top_to_lines_schema():
    raw = (FIX / "rec_sample.txt").read_text().splitlines()[-1]
    t = parse_top_line(raw)
    boot_epoch = 1_785_000_000.0
    lines = top_to_lines("D-005-02408", t, boot_epoch)
    text = "\n".join(lines)

    assert "stick_mem_total_bytes,serial=D-005-02408 value=3932655616" in text
    assert ("stick_proc_pss_bytes,proc=com.dolphin_us.dolphinstore,"
            "serial=D-005-02408 value=719986688") in text
    # CPU lands in SECONDS so no query needs to know the device's CLK_TCK:
    # 58900 ticks / 100 = 589 s
    assert ("stick_proc_cpu_seconds_total,mode=user,"
            "proc=com.dolphin_us.dolphinstore,serial=D-005-02408 value=589.0") in text
    # system_server had no smaps_rollup — no PSS series at all, rather than a 0
    assert "proc=system_server" in text
    assert "stick_proc_pss_bytes,proc=system_server" not in text
    assert "stick_proc_rss_bytes,proc=system_server" in text

    expect_ns = str(int((boot_epoch + t.uptime) * 1e9))
    assert all(l.endswith(expect_ns) for l in lines)


def test_renderer_pool_is_summed_not_collided():
    """normalize_proc collapses :sandboxed_processN by design, so several rows
    arrive under one name. Identical labels at one timestamp are ONE series in
    VM — emitting them separately would last-write-wins away every renderer but
    the last, silently understating the stacked total."""
    t = parse_top_line("#top|10.0|100|100|"
                       "com.foo,100,400000,390000,1000,100;"
                       "com.foo:sandboxed_process0,101,100000,95000,500,50;"
                       "com.foo:sandboxed_process1,102,120000,110000,600,60;")
    lines = top_to_lines("S", t, 0.0)
    pss = [l for l in lines if l.startswith("stick_proc_pss_bytes")]
    assert len(pss) == 2                                   # not 3 — merged
    renderer = next(l for l in pss if "sandboxed_processN" in l)
    assert f"value={(95000 + 110000) * 1024}" in renderer   # summed, not 110000
    cpu = next(l for l in lines
               if "sandboxed_processN" in l and "mode=user" in l)
    assert "value=11.0" in cpu                              # (500 + 600) / 100


def test_partial_pss_in_a_merged_group_is_dropped_not_understated():
    t = parse_top_line("#top|10.0|100|100|"
                       "com.foo:sandboxed_process0,101,100000,95000,500,50;"
                       "com.foo:sandboxed_process1,102,120000,0,600,60;")
    text = "\n".join(top_to_lines("S", t, 0.0))
    assert "stick_proc_pss_bytes" not in text               # would be a half-truth
    assert f"stick_proc_rss_bytes,proc=com.foo:sandboxed_processN,serial=S " \
           f"value={220000 * 1024}" in text


def test_top_cpu_seconds_respects_device_clk_tck():
    # a device reporting CLK_TCK=250 must not be read as if it were 100
    t = parse_top_line("#top|10.0|100|250|app,1,1024,1024,500,250;")
    text = "\n".join(top_to_lines("S", t, 0.0))
    assert "mode=user,proc=app,serial=S value=2.0" in text
    assert "mode=system,proc=app,serial=S value=1.0" in text


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


# ---- discovery (§2.3, relaxed for the test bench) ------------------------------

def test_sweep_finds_only_listening_hosts():
    import asyncio, socket
    from beacon_collector import discovery

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    open_ = asyncio.run(discovery._port_open("127.0.0.1", port, 1.0))
    shut = asyncio.run(discovery._port_open("127.0.0.1", port + 1, 0.3))
    srv.close()
    assert open_ == f"127.0.0.1:{port}"
    assert shut is None


def test_sweep_rejects_a_bad_subnet_instead_of_raising():
    import asyncio
    from beacon_collector import discovery
    assert asyncio.run(discovery.sweep("not-a-subnet")) == []


def test_discovery_config_requires_a_subnet_when_enabled(tmp_path):
    import pytest, yaml
    from beacon_collector.config import load_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    p = cfg_dir / "beacon.yaml"
    base = {"nuc_id": "n", "devices": {},
            "discovery": {"enabled": True}}          # subnet missing
    p.write_text(yaml.safe_dump(base))
    with pytest.raises(ValueError, match="discovery.subnet"):
        load_config(p)

    base["discovery"]["subnet"] = "192.168.0.0/24"
    p.write_text(yaml.safe_dump(base))
    assert load_config(p).discovery.subnet == "192.168.0.0/24"


def test_discovery_is_off_unless_asked(tmp_path):
    import yaml
    from beacon_collector.config import load_config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    p = cfg_dir / "beacon.yaml"
    p.write_text(yaml.safe_dump({"nuc_id": "n", "devices": {}}))
    assert load_config(p).discovery.enabled is False


def test_relocating_a_device_changes_its_address_with_no_restart():
    """DHCP moved the lease. address is a property over host, so mutating host
    is enough — the supervisor's next reconnect cycle uses the new address and
    keeps its cursors."""
    from beacon_collector.config import DeviceConfig
    d = DeviceConfig(serial="D-005-02408", host="192.168.0.100")
    assert d.address == "192.168.0.100:5555"
    d.host = "192.168.0.107"
    assert d.address == "192.168.0.107:5555"


def test_applog_pump_is_not_spawned_without_an_app_package():
    """A pump that returns immediately would win asyncio.wait(FIRST_COMPLETED)
    and tear down every other stream, spinning a reconnect loop that collects
    nothing — the exact failure mode of the 2026-07-28 logcat bug. So the task
    must not be created at all."""
    import inspect
    from beacon_collector import supervisor
    src = inspect.getsource(supervisor.DeviceSupervisor._connect_cycle)
    assert "if self.app_package:" in src
    body = inspect.getsource(supervisor.DeviceSupervisor._pump_applog)
    assert "return" not in body.split("async for")[0]


# ---- device console (beacon devices / beacon serve) ----------------------------

def _reg(tmp_path):
    from beacon_collector.registry import Registry
    return Registry(tmp_path / "r.db")


def _cfg(tmp_path, devices=None, disc=None):
    from beacon_collector.config import Config, DiscoveryConfig
    return Config(nuc_id="nuc-a", registry_db=tmp_path, spool_dir=tmp_path,
                  parquet_dir=tmp_path, victoriametrics="", loki="",
                  s3_bucket=None, s3_region="us-east-1", app_package="com.dolphin",
                  devices=devices or {}, discovery=disc or DiscoveryConfig())


def test_override_roundtrip_and_partial_update(tmp_path):
    r = _reg(tmp_path)
    r.upsert_device("S1", "10.0.0.5:5555", "nuc-a", "from-config")
    r.set_override("S1", friendly_name="bench-1")
    r.set_override("S1", app_package="")        # must not clear the name set above
    o = r.overrides()["S1"]
    assert o["friendly_name"] == "bench-1" and o["app_package"] == "" and o["skip"] is False
    assert r.list_devices()[0]["friendly_name"] == "bench-1"   # override wins


def test_app_package_sentinel_clears_back_to_inherit(tmp_path):
    r = _reg(tmp_path)
    r.set_override("S1", app_package="com.foo")
    assert r.overrides()["S1"]["app_package"] == "com.foo"
    r.set_override("S1", app_package="-")       # "-" = inherit the global again
    assert r.overrides()["S1"]["app_package"] is None
    r.set_override("S1", app_package="")        # "" = tail no app log
    assert r.overrides()["S1"]["app_package"] == ""


def test_console_shows_declared_devices_that_never_connected(tmp_path):
    """A stick configured but never arrived must still appear — that absence is
    exactly what you open the console to notice."""
    from beacon_collector.webui import _Handler
    from beacon_collector.config import DeviceConfig, DiscoveryConfig

    cfg = _cfg(tmp_path,
               devices={"NEVER-SEEN": DeviceConfig(serial="NEVER-SEEN",
                                                   host="10.0.0.9", nuc="nuc-a")},
               disc=DiscoveryConfig(enabled=True, subnet="10.0.0.0/24"))
    _Handler.registry, _Handler.cfg = _reg(tmp_path), cfg
    row = next(d for d in _Handler._snapshot(_Handler)["devices"]
               if d["serial"] == "NEVER-SEEN")
    assert row["last_seen"] is None and row["declared"] is True

    # a skipped serial in neither the config nor devices still surfaces
    _Handler.registry.set_override("GHOST", skip=True)
    assert any(d["serial"] == "GHOST" and d["skip"]
               for d in _Handler._snapshot(_Handler)["devices"])


def test_webui_writes_over_http(tmp_path):
    import json as _json, threading, urllib.request
    from http.server import HTTPServer
    from beacon_collector.webui import _Handler

    _Handler.registry, _Handler.cfg = _reg(tmp_path), _cfg(tmp_path)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}/api/devices",
        data=_json.dumps({"serial": "S9", "friendly_name": "x", "skip": True}).encode(),
        headers={"content-type": "application/json"})

    # The HANDLER must run in the thread that opened the registry — sqlite3
    # connections are thread-bound, which is why serve() uses HTTPServer and
    # not ThreadingHTTPServer. So drive the request from the background thread
    # and keep handle_request here; inverting the two reproduces the
    # ProgrammingError this arrangement exists to avoid.
    got = {}

    def client():
        got["body"] = urllib.request.urlopen(req, timeout=5).read()

    t = threading.Thread(target=client, daemon=True)
    t.start()
    srv.handle_request()
    t.join(timeout=5)
    srv.server_close()

    assert _json.loads(got["body"])["ok"] is True
    assert _Handler.registry.overrides()["S9"]["skip"] is True


def test_last_seen_is_refreshed_while_streaming(tmp_path):
    """upsert_device runs once per connect cycle and a session lasts hours, so
    without periodic touching every console row reads 'idle' for a device that
    is streaming fine."""
    import inspect
    from beacon_collector import supervisor
    src = inspect.getsource(supervisor.DeviceSupervisor._heartbeat)
    assert "touch_device" in src, "heartbeat must refresh last_seen"

    r = _reg(tmp_path)
    r.upsert_device("S1", "10.0.0.5:5555", "nuc-a", "")
    before = r.list_devices()[0]["last_seen"]
    import time as _t
    _t.sleep(0.01)
    r.touch_device("S1")
    assert r.list_devices()[0]["last_seen"] > before


def test_startup_prefers_the_last_address_that_actually_worked(tmp_path):
    """A stale YAML host: costs up to one discovery interval of dead retries on
    every restart. D-005-02408's lease moved .100 -> .57 while the config still
    said .100."""
    from beacon_collector.cli import _seed_addresses
    from beacon_collector.config import DeviceConfig

    r = _reg(tmp_path)
    r.upsert_device("D-005-02408", "192.168.0.57:5555", "nuc-a", "")
    dev = DeviceConfig(serial="D-005-02408", host="192.168.0.100", nuc="nuc-a")
    never = DeviceConfig(serial="NEVER-SEEN", host="192.168.0.9", nuc="nuc-a")

    _seed_addresses([dev, never], r)
    assert dev.address == "192.168.0.57:5555"      # registry wins
    assert never.address == "192.168.0.9:5555"     # config seeds the unseen


def test_seeding_survives_a_broken_registry(tmp_path):
    """Never let a control-plane read failure stop the collector starting."""
    from beacon_collector.cli import _seed_addresses
    from beacon_collector.config import DeviceConfig

    class Broken:
        def list_devices(self): raise RuntimeError("locked")

    dev = DeviceConfig(serial="S1", host="10.0.0.1", nuc="n")
    _seed_addresses([dev], Broken())
    assert dev.address == "10.0.0.1:5555"


def test_a_hand_edited_host_wins_when_discovery_is_off(tmp_path, monkeypatch):
    """With discovery off the YAML is the only source of truth, so seeding must
    not quietly override a deliberate `host:` edit."""
    import asyncio, sys
    from beacon_collector import cli
    from beacon_collector.config import DeviceConfig

    seeded = []
    monkeypatch.setattr(cli, "_seed_addresses",
                        lambda devs, reg: seeded.append(devs))
    cfg = _cfg(tmp_path, devices={"S1": DeviceConfig(serial="S1", host="10.0.0.1",
                                                     nuc="nuc-a")})
    cfg.registry_db = tmp_path / "r.db"
    cfg.spool_dir = tmp_path / "spool"
    cfg.parquet_dir = tmp_path / "pq"
    assert cfg.discovery.enabled is False

    async def stop_immediately(*a, **k):
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "gather", stop_immediately)
    try:
        asyncio.run(cli._run(cfg))
    except Exception:
        pass
    assert seeded == [], "seeding must not run with discovery disabled"
