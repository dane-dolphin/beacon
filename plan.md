# Build Brief — Dolphin Stick Lab Telemetry Platform

**Status:** ready to build. Supersedes `HANDOVER_STICK_TELEMETRY.md` and `PLAN_STICK_TELEMETRY_V1.md` wherever they conflict.
**Written:** 2026-07-27. Every number marked **[MEASURED]** was taken from a real device that day; treat unmarked figures as estimates.
**Reference device:** `D-005-02408` at `192.168.0.106:5555`.

> **To the Claude picking this up:** you do not have the originating conversation. Everything needed is here. §1 is verified ground truth — trust it over your own assumptions about Android. §12 lists what is still unknown; ask rather than guess. Where this document says a thing was *validated on hardware*, it means someone ran it and watched it work.

---

## 0. What this system is for

A **lab canary rig** that catches issues before they reach production. Not primarily a freeze debugger — that's one workload it runs.

Four questions it must answer:

1. **Is this build worse than the last one?** (regression detection across app builds — the primary purpose)
2. **Is anything abnormal right now?** (live health + alerting)
3. **What happened at time T?** (forensic reconstruction)
4. **How far can a stick be pushed?** (capacity — camera analytics memory/thermal headroom)

All four sit on one collection layer. They differ only in retention and resolution.

**Fleet:** 10–20 Dolphin Sticks. Mixed model — some permanently powered as canaries, others borrowed for specific experiments. Devices appearing and disappearing is normal, not an error.

**Proving workload:** the freeze A/B (§11). Not the goal, but the only fully-specified experiment available, so it validates the rig.

---

## 1. Verified device facts [MEASURED]

### 1.1 Identity and access
```
ro.serialno              D-005-02408
ro.product.model         Dolphin Stick        ro.product.device  ohm
ro.build.version.release 11                   ro.build.version.sdk 30
ro.build.type            userdebug            ro.debuggable      1
ro.build.fingerprint     Droidlogic/ohm/ohm:11/RD2A.211001.002/eng.yxtrd0.20240627.172353:userdebug/test-keys
```
- **`adb root` works** → `uid=0(root)`. Required for mali debugfs and dmesg.
- 4 CPU cores. `MemTotal 3840484 kB` (3.84 GB). `CmaTotal 692224 kB`.
- Thermal zones: **only two** — `thermal_zone0 = soc_thermal`, `thermal_zone1 = ddr_thermal`.
- Display: 50 Hz panel (`VSYNC period: 20000000 ns`). `Display 0: no identification data` — no HDMI EDID when nothing is attached.
- MDM packages present: `io.shoonya.shoonyadpc`, `io.esper.plugin.supervisor`, `io.esper.remoteviewer`, `io.shoonya.helper`, + 2 oculus supervisor plugins.

### 1.2 adb-over-TCP persistence — MDM-dependent
`persist.adb.tcp.port` is **empty**. `service.adb.tcp.port=5555` is set at runtime and appears in **no** `build.prop`. The Esper/shoonya device-owner re-arms TCP after each boot.

**Decision:** leave this alone. The collector retries periodically. **But the supervisor must treat the post-reboot reachability gap as expected** — a silence beginning near the ~22:00 MDM reboot that ends with a device whose uptime has reset is a *reboot*, never a freeze.

### 1.3 GPU memory — the leak indicator
`/sys/kernel/debug/mali0/gpu_memory`, readable **as root only**:
```
mali0            total used_pages      69569
----------------------------------------------------
kctx             pid              used_pages
----------------------------------------------------
00000000bbddc13f       1689      67644     <- com.dolphin_us.dolphinstore
00000000f2eeeaed       2840        198
000000000063497d56      366        389     <- /system/bin/surfaceflinger
```
**Pages × 4096 = bytes.** Parse: line containing `mali0` → total; subsequent 3-column rows → `(kctx, pid, used_pages)`.

Also present in that directory, unexplored: `job_fault` (root-only, blocking read), `ctx/`, `regs_history`, `serialize_jobs`.

**Cross-check:** `dumpsys meminfo com.dolphin_us.dolphinstore` reported `GL mtrack 296612 kB` (290 MB) while `mali0` showed 67644 pages (264 MB) for the same pid moments apart. **The cheap 1 Hz debugfs gauge is a valid proxy for the expensive dumpsys number.** Collect both; only the cheap one needs to be fast.

**GPU memory attribution is to the app process (1689), not the WebView sandbox child (2533).** Monitor the app pid.

### 1.4 The leak is invisible in short windows [MEASURED]
Six samples, 10 s apart, DPlayer GPU pages:
```
uptime=695s  73803 pages (288 MB)   temp=63.2C
uptime=706s  69071 pages (269 MB)   temp=63.5C
uptime=717s  70841 pages (276 MB)   temp=63.7C
uptime=728s  68420 pages (267 MB)   temp=63.7C
uptime=739s  66278 pages (258 MB)   temp=63.8C
uptime=750s  69618 pages (271 MB)   temp=64.0C
```
**Sawtooth, 258–288 MB, ±15%, no trend over 55 s.** Tracks the WebView ping-pong.

Two consequences, both load-bearing:
- **Absolute-threshold alerting will false-fire constantly.** Detection must be **slope or rolling-envelope (max) over hours**.
- **Never average this metric when downsampling.** Averaging a sawtooth destroys the envelope, which *is* the signal. Rollups keep `max`/`min`/`last`.

At ~270 MB average **12 minutes after boot** against a ~320 MB observed ceiling, baseline already sits near the ceiling.

### 1.5 GPU faults are live and real [MEASURED]
Captured during the probe, ~40 s after a clean check:
```
[502.604062] mali fe400000.bifrost: error detected from slot 0, job status 0x00000058 (DATA_INVALID_FAULT)
[502.604097] mali fe400000.bifrost: t6xx: GPU fault 0x58 from job slot 0
```
Full signature set to grep in `dmesg`:
```
JOB_READ_FAULT | DATA_INVALID_FAULT | Unhandled Page fault
Job Hard-Stopped | Reset complete | next index above max
```
Interpretation: saturated GPU memory + repeated read-faults + a `Job Hard-Stopped` that does **not** recover → surface lost → HDMI black → app hangs alive (no crash, no OOM-kill) → needs reboot.

### 1.6 Thermal runaway [MEASURED]
`soc_thermal` over ~50 minutes of observation: **60.1 → 63.8 → 65.3 → 76.3 °C**, `ddr_thermal` 78.6 °C, `loadavg` 5.5–6.2 on 4 cores. Monotonic climb under sustained load, ~13 °C past the 65 °C critical threshold, sustained for an hour. **Worth investigating independently of this rig.**

### 1.7 Log buffers — remediation ALREADY APPLIED
**Before:** ring buffers 256 KiB each, `main` at 238/256 KiB (93% full), and `chatty` actively pruning DPlayer:
```
I chatty : uid=10067(com.dolphin_us.dolphinstore) mqt_js expire 77 lines
```
**[MEASURED] loss: 2689 lines dropped vs 5719 delivered over 659 s = 32.0%.** Chatty prunes the *highest-volume UID first*, which is DPlayer — exactly backwards for a rig whose subject is the noisiest app.

**Applied to `D-005-02408` on 2026-07-27:**
```bash
adb -s $D root
adb -s $D shell setprop persist.logd.size 4M      # persists across reboot
adb -s $D shell setprop persist.logd.filter ""    # disables chatty worst-UID pruning
adb -s $D logcat -b all -G 4M                     # immediate effect, this boot only
adb -s $D shell logd --reinit
```
**Verified: all four buffers at 4 MiB, 0 chatty events, 0 lines dropped** in a 61 s sample.

**MUST be part of device onboarding for every unit, and verified rather than assumed.** `persist.logd.size` taking effect at boot has **not yet been confirmed across a reboot** — check this.

Two distinct jobs: buffer size buys *gap tolerance* (a 30 s hiccup no longer costs the window); chatty-off removes the *selective penalty* against the monitored app.

### 1.8 Heartbeat — excellent [MEASURED]
491 `ReactNativeJS` timestamps: **median gap 2 ms, p95 2.0 s, max 3.0 s, zero gaps > 5 s.** Measured *while* chatty was still eating 32%, so it only improves.

**A 30 s stall threshold is safe and fast.** Heartbeat = age of the most recent `ReactNativeJS` line vs the device's own clock. Never compare across devices — clocks drift (this unit was in exact sync with the host; the handover reports units 2 h apart).

### 1.9 The app already emits rich telemetry [MEASURED]
This is the single most valuable finding. DPlayer's `ReactNativeJS` output contains:

| Field | Example | Use |
|---|---|---|
| `vc` / `versionCode` | `183` | **app build → automatic tagging. No manual labelling needed.** |
| `creativeId` | `azdd-PRODUCT-1778274790998` | per-creative attribution |
| `contentId`, `campaignId` | `PRODUCT-1776822280013` | grouping |
| `url` / `mediaPath` | `https://d2wuawlurlul47.cloudfront.net/weather/?version=3` | **the A/B lever may be this `?version=` param** |
| `durationSecs` | `5` | dwell time — see §11 |
| `mime` | `weblink` | low-cardinality, safe as a metric label |
| `exchangeType` | `OW_DL` | low-cardinality |
| `WEB_VIEW_1` / `WEB_VIEW_2` | `PlayerInner:: setNextAd: Setting Active view as` | double-buffer ping-pong |
| `getHdmiStatus` | `false` | **display ground truth from the app itself** |
| `getStorageInfo` | `external_free`, `internal_free` | storage |
| `ramUsageInfo` / `deviceRamUsage` | `availMem`, `lowMemory` | app's own memory view |
| `dId` / `venueCode` / `installationId` | `DD-H33CBP`, `63718089` | identity cross-ref |

Module prefixes seen: `Scheduler`, `PlayerInner`, `AdUtility`, `CloudLogger`, `AdOwnMgr`, `AdsProvider`, `ADLOGS`, `AdExchangeMgr`, `Utils`, `SyncMgr`, `WsMgr`, `StatusHelper`, `AssetStore`, `_processLogBeforeSending`.

**The app already ships logs to AWS:**
```
url: 'https://tvaqma11rf.execute-api.us-east-1.amazonaws.com/Prod/devicelogs'
```
An API Gateway in **us-east-1**. **Find out what's behind it before building a parallel pipeline** — it may already hold months of app-side history, and it's the natural join target.

### 1.10 Log volume and composition [MEASURED]
- **238 MB/device/day raw**, 29.7 lines/s.
- **92.8% level `I`**, 90.6% tag `ReactNativeJS`. Errors only **2.9% (7 MB/day)**.
- **87% of lines are repeats** of an earlier line (ignoring timestamp/pid): 1807 total, 238 distinct.
- **The volume is encoding, not information.** DPlayer `console.log`s whole objects; React Native pretty-prints each across 20–30 physical lines. 89% of the largest bucket (147 MB/day) is JSON *continuation lines*. One creative-change event costs ~1 KB of text to carry ~150 bytes of payload, with the same URL repeated four times.

Top repeated error: `ocessService0: failed to create Unix domain socket: Operation not permitted` — **31× in 61 s**, 4.8 of the 7 MB/day error budget. Benign WebView sandbox noise.

**DropBox is already dirty:** 3× `system_app_crash`, 3× `system_server_wtf`, 6× `system_server_strictmode` within ~2 min of boot. `AndroidRuntime` traces point at `DevicePolicyManagerService.getFactoryResetProtectionPolicy` — **MDM instability, distinct from DPlayer**. Crash attribution must be per-package or this noise swamps real signal.

### 1.11 Timestamps — three clocks, none agree
- **`dmesg` is kernel-monotonic**: `[502.604062]`.
- **logcat's kernel buffer carries bogus wall-clock**: `12-31 18:00:05` (pre-NTP boot).
- **logcat main/system carry correct wall-clock but NO YEAR**: `07-27 12:47:32.929`.

**Rules:**
- Capture `boot_epoch = wall_now − /proc/uptime` **per device, per boot**. Store it. It converts dmesg monotonic → wall clock.
- Stamp every record with **collector receive-time as primary**, device time as secondary.
- Record host↔device offset at each connect.
- **Never store a year-less timestamp.** This is the exact bug in the existing `logging_pipeline`: its `cleanup()` runs `DELETE FROM device_logs WHERE log_ts < datetime('now',?)` against `'07-27 10:41'`-style values, so the comparison is *always true* and **the job wipes the entire log table every time it runs**. Its analyzer's `log_ts >= datetime('now','-60 minutes')` is always *false*, so error logs never reach the LLM. Do not repeat this.

### 1.12 The detached on-device recorder — VALIDATED ON HARDWARE
The critical architectural bet, tested end to end.

A plain `adb shell 'while true; ...'` **dies with the connection** (SIGHUP) — exactly when you need it. Launching it **detached** survives:

```bash
adb -s $D shell 'setsid sh /data/local/tmp/rec.sh >/dev/null 2>&1 </dev/null &'
```

**Test result:** 4 samples written, `adb disconnect`, 8-second outage, reconnect → **same PID still alive, 13 samples, continuous across the gap.**

Available on device: `setsid`, `tee`, `tail`, `nohup`, `timeout`, `toybox`. `/data/local/tmp` writable. `/data` has ~24 GB free.

Collector reads it with `tail -F` over adb. Recorder lifetime is independent of the connection: **stream dies, file keeps growing.** That's what distinguishes a network drop from a device freeze.

Since 5555 survives reboots (§1.2), the collector re-launches the recorder on each reconnect.

**Measured record: 184 bytes** for uptime, mali total + app pages, app PID, MemAvailable, CmaFree, both thermal zones, loadavg, 4× per-core CPU, net rx/tx:
```
3208.85|70597|1689|68672|2312732|580684|76200|78600|6.16 5.98 5.77|588450,204927,402869;...|9820117,5034536;
```
**Shell-loop cadence is ~1.1 s, not 1.0 s.** Stamp from device uptime; never infer time from assumed cadence.

### 1.13 No camera on this unit
`dumpsys media.camera` → `Number of camera devices: 0`. bAgent (`com.trill.bagent`) was not running. **The camera module cannot be validated on `D-005-02408`** — a camera-equipped unit is needed.

### 1.14 Display liveness
`dumpsys SurfaceFlinger | grep -oE 'count=[0-9]+'` → `app: state=VSync VSyncState={displayId=0, count=16498}`. Monotonic counter, advanced ~22/s on a 50 Hz panel. Usable as a liveness signal. Too expensive for 1 Hz — collect at 10 s.

### 1.15 The app's own file log
`/sdcard/Android/data/com.dolphin_us.dolphinstore/files/Logs/Dolphin_File.log[.01–.08]`, ~6 MB each, ~54 MB retained.

**Written by the app directly, so chatty never touches it — it is strictly more complete than logcat.** Treat as an authoritative source, not merely freeze evidence. Note the handover's "~40 min/file, ~7 h retained" is wrong: retention is a function of log rate; observed archives dated 4 days back.

### 1.16 Compression and search performance [MEASURED]
On 184 MB of real logcat:

| codec | ratio | decompress + substring search | throughput |
|---|---|---|---|
| gzip | **8.9:1** | 0.26 s | **700 MB/s** |
| lz4 | 5.5:1 | 0.23 s | 816 MB/s |
| *plain grep* | — | 0.15 s | 1261 MB/s |

**Decompression costs ~45% over plain grep, not 10×.** Use **~10:1 and ~400 MB/s** as planning figures for Graviton2.

*(A zstd run showed 70:1 — an artifact of a synthetic corpus built by repeating one file. Real streaming logs would be ~10–12:1. Do not plan on 70:1.)*

---

## 2. Architecture

```
Each stick (rooted, LAN, adb TCP 5555)
  ├─ [A] detached recorder → bounded ring file on /data     ~1 Hz   VALIDATED
  ├─ [B] adb logcat -v threadtime -b all                    stream
  ├─ [C] adb shell dmesg -w                                 stream
  ├─ [D] Dolphin_File.log                                   incremental tail
  └─ [E] dumpsys tier                                       30–60 s, bursts to 1 Hz
                 │
     10 sticks ──┴── NUC-A ─┐
                            ├──► EC2 t4g.medium (us-east-1)
     10 sticks ───── NUC-B ─┘         ├─ VictoriaMetrics  (EBS, 1 yr, full 1 Hz)
                                      ├─ Loki             (S3 chunks, 30 d, filtered text)
                                      ├─ Grafana          (dashboards + alerts)
                                      └─ DuckDB           (ad-hoc SQL over S3 Parquet)
                                              │
                                      S3 ─────┴─ Parquet (EVERYTHING, unfiltered, forever)
                                                 Loki chunks
                                                 vmbackup (nightly)
                                                 incident bundles
```

**EC2 cannot collect.** No route to a stick behind NAT; adb must originate on the LAN. NUCs do adb, parse, filter, buffer, and forensic pulls. EC2 stores, serves, analyses.

### 2.1 Four things must stay on the NUC — physics, not preference
1. **adb supervision** — cloud has no route to the devices.
2. **Forensic pull** — `dumpsys dropbox`, `SYSTEM_LAST_KMSG`, the ring file all need a live adb session.
3. **Durable local buffer** — must survive an uplink outage *and* a NUC restart, with offset tracking so nothing is double-sent or lost.
4. **A local watchdog** that triggers the forensic pull without waiting for cloud. During an outage is exactly when evidence is created.

### 2.2 What cloud uniquely provides
**Detecting a dead NUC.** Each NUC heartbeats to EC2; cloud alerts on absence. NUC-side monitoring dies with the NUC. This is the answer to "how dependable are the NUCs".

### 2.3 Device→NUC ownership
**Explicit assignment in the registry.** If both NUCs connect to the same stick you get duplicate streams, doubled device load, and doubled ingest. Cloud should alert on duplicate ingestion for a serial.

### 2.4 Storage — three tiers, decided
| tier | contents | retention | purpose |
|---|---|---|---|
| **VictoriaMetrics** (EBS) | numeric series, full 1 Hz | **1 year** (38 GB) | dashboards, alerting |
| **Loki** (S3 chunks) | **filtered** text | **30 days** (~16 GB in S3) | interactive search in Grafana |
| **Parquet** (S3) | **everything, unfiltered**, one row per log line | **forever** | SQL analysis, archive, recovery |

**There is no coarse/downsampled tier.** It was designed away: 90 days of full 1 Hz is only 9.4 GB, and **VictoriaMetrics open-source has no downsampling** (enterprise feature) — a coarse tier would mean custom rollup jobs solving a non-problem.

**Because Parquet holds everything unfiltered forever, filtering is no longer irreversible.** It only decides what enters the fast interactive tier. Any filter mistake is recoverable with a query.

### 2.5 EBS budget (100 GB gp3)
| | |
|---|---|
| OS + Docker | 10 GB |
| Grafana | 1 GB |
| VictoriaMetrics, 1 yr full 1 Hz, 20 devices | 38 GB |
| Loki local WAL/cache (chunks in S3) | 4 GB |
| optional Parquet working cache | 10 GB |
| **total** | **63 GB — 37 GB free** |

gp3 gives 3000 IOPS / 125 MB/s at **any** size, and **resizes online**. Instance type changes are stop→change→start, ~2 min, EBS untouched. Both decisions are reversible.

### 2.6 Cost
| | |
|---|---|
| t4g.medium (4 GB, 2 vCPU, ARM) | $24.53/mo |
| EBS gp3 100 GB | $8.00/mo |
| S3 (Parquet + Loki chunks + vmbackup) | ~$1.50/mo, growing ~$0.33/mo per month accumulated |
| vmbackup storage | $0.87/mo |
| **total** | **≈ $35/mo**, or **≈ $25/mo** on a 1-year Savings Plan |

RAM steady state is ~1.1 GB of 4 GB. DuckDB is the only real spike — **set `SET memory_limit='1.5GB'`** so it spills rather than OOMs, and **configure a 4 GB swap file**. If it proves tight, t4g.large (8 GB, same 2 vCPU) is $49/mo.

t4g are **burstable**. Sustained DuckDB work can burn CPU credits and throttle — that's the symptom to recognise; `m7g.medium` is the non-burstable escape.

### 2.7 Technology choices and rationale
- **Collector: Python.** 10–20 devices doesn't justify Go's concurrency story; iteration speed matters more for a lab tool. Must be **portable** — no Windows-only paths (the existing code hardcodes `adb.exe` and `%LOCALAPPDATA%`).
- **Emit InfluxDB line protocol.** VictoriaMetrics accepts it natively on `/write`, so the metrics backend stays swappable without touching collector code.
- **VictoriaMetrics over InfluxDB 2.7.** Influx + Loki + Grafana peaks at 1220–3150 MB; VM brings that to 570–1550 MB. VM also compresses better, handles cardinality far better, and Influx 2.x's Flux is a dying branch.
- **DuckDB over Athena.** Same Parquet either way. Scans are small (a 30-day, 4-column build comparison touches 0.48 GB), DuckDB is free per query and typically *faster* than Athena at this size — Athena's 1–3 s fixed overhead exceeds the whole query. Keep Athena as a later add: one `CREATE EXTERNAL TABLE` over the same S3 prefix, no data migration.
- **Loki over Elasticsearch.** Elastic needs ~3 GB RAM (forcing t4g.large, +$24.53/mo) and stores logs at ~1.7× raw vs Loki's ~0.1×. Loki is a native Grafana datasource, which is what makes click-from-graph-to-logs work. LogQL for this work is grep: `{serial="D-005-02408"} |= "mali"`.

---

## 3. Log processing pipeline (on the NUC)

### 3.1 Parse
Validated regex for `-v threadtime`:
```python
LOGCAT = re.compile(
    r'^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?):\s?(.*)$'
)   # -> ts_noyear, pid, tid, level, tag, message
```
Add the year from collector receive-time; handle the Dec→Jan rollover.

### 3.2 Reassemble multi-line JSON — use the timestamp
All 12 lines of a creative-change event share **timestamp `12:47:32.929`, pid `1689`, tid `2034`, tag `ReactNativeJS`**. So the reliable rule is:

> **Same (timestamp, pid, tid, tag) = one logical event.**

Far more robust than indentation heuristics. Allow a few ms of tolerance for events straddling a millisecond boundary, and require continuations to be continuation-shaped (`key: value`, `{`, `}`, or indented).

### 3.3 Four tiers [MEASURED reduction]
| tier | action | volume |
|---|---|---|
| 1. `E`/`F` | ship verbatim, **dedup by normalized signature** (3 exemplars + counter) | 7.0 → **1.7 MB/day** |
| 2. `W` | ship verbatim | **0.5 MB/day** |
| 3. app JSON blocks | reassemble → parse → one structured row, discard text | 175 → **5.0 MB/day** |
| 4. system noise | drop by tag denylist | 14.9 → **0** |
| — | 1 Hz numeric series | **5.2 MB/day** |
| | **shipped to the interactive tier** | **12.4 MB/day — 19:1** |

20 devices: **0.25 GB/day, 7.4 GB/month**.

Tag denylist (all redundant with data collected properly elsewhere):
```
ThermalService  AbstractTask  NetworkScheduler.Stats  WakeLock  GCM  TaskProcessor
DeviceStatusUpdateTask  EsperRetryInterceptor  EsperSharedDataContentProviderHelper
MqttAndroidClientConnectionManager  wifi@1.0-servic  PackageWatchdog
ExplicitHealthCheckController  SystemServerTimingAsync  CompatibilityChangeReporter
ConnectivityService  ExperimentPackageManage  StrictMode
```
(`ThermalService` logs `CPU temperatures: [76.1]` every 2 s — we read `/sys` at 1 Hz.)

### 3.4 Filtering safety rules — non-negotiable
1. **Denylist, never allowlist.** Default is *ship*. An allowlist silently discards every new thing the app starts logging — precisely what you want to see after a build change.
2. **An `unparsed` bucket that always ships**, plus an alert when the unparsed *rate* rises. A format change then arrives as an alert, not a hole.
3. **Sample rather than zero** for denylisted tags — 1 in 100 keeps the tag alive.
4. **Ship the dedup counter as a metric**, so "this error went 30/min → 3000/min" stays visible.
5. **Everything unfiltered still goes to Parquet in S3.** Filtering only gates the interactive tier.
6. **Roll out permissively.** Ship nearly everything at first; tighten with evidence from real data, not from this denylist.

### 3.5 Idempotency — Parquet needs deterministic naming
The collector buffers during outages and resends, so **duplicate sends will happen**.
- **VictoriaMetrics is naturally idempotent** — same series + timestamp = last write wins. Resends are harmless.
- **Parquet is not.** Naive appending yields duplicate rows and every downstream count is wrong.

**Fix:** one object per device per hour at a path derived from the data — `metrics/dt=2026-07-27/hour=14/serial=D-005-02408.parquet`. A retry overwrites the same key. Nightly compaction merges hours into a day file.

### 3.6 S3 layout — keeps Athena available for free
```
s3://<bucket>/metrics/dt=2026-07-27/part-000.parquet
s3://<bucket>/logs/dt=2026-07-27/part-000.parquet
s3://<bucket>/events/dt=2026-07-27/part-000.parquet
s3://<bucket>/incidents/<incident_id>/bundle.tar.gz
s3://<bucket>/loki/...
s3://<bucket>/vmbackup/...
```
Rules:
1. **Hive-style `key=value` partitions** — Athena reads them natively with partition projection; DuckDB with `hive_partitioning=1`.
2. **Partition by date only; `serial` is a column.** With 20 devices a day is ~160 MB — cheap to scan whole. Partitioning by serial fragments files for no benefit.
3. **Compact to ~160 MB/day files.** Hourly-per-device gives 480 files/day (175k/year) at 0.3 MB — Athena crawls on that.
4. **Real INT64 TIMESTAMP logical type, never strings.** Enables predicate pushdown; avoids repeating §1.11's bug.
5. **zstd** compression (Athena engine v3 + DuckDB both support it); snappy is the conservative fallback.

---

## 4. Collection tiers and cadence

| Signal | Source | Tier |
|---|---|---|
| logcat (all buffers), dmesg, app file log | streams | **continuous, never sampled** |
| mali total + per-pid pages | `/sys/kernel/debug/mali0/gpu_memory` | **1 Hz** (recorder) |
| MemAvailable, CmaFree | `/proc/meminfo` | 1 Hz |
| per-core CPU, loadavg | `/proc/stat`, `/proc/loadavg` | 1 Hz |
| net rx/tx | `/proc/net/dev` | 1 Hz |
| both thermal zones | `/sys/class/thermal/thermal_zone{0,1}/temp` | 1 Hz |
| app PID + process uptime | `pidof` | 1 Hz |
| display liveness | `dumpsys SurfaceFlinger` vsync count | 10 s |
| `GL mtrack`, Native/Dalvik/TOTAL PSS | `dumpsys meminfo <pkg>` | 30–60 s, **burst to 1 Hz on anomaly** |
| fd count, thread count, WebView count | `/proc/<pid>/fd`, meminfo | 30–60 s |
| storage | `df` | 30–60 s |
| boot reason | `getprop sys.boot.reason` | on connect |
| crashes/ANRs/tombstones | `dumpsys dropbox` | event + post-reboot |

**Toggleable modules** (per-device and per-run — a week-long canary shouldn't pay for camera dumps it doesn't use):
- `gpu` — default on
- `camera` — `dumpsys media.camera` + camera log tags. **Externally observable: camera open/closed, holding package, resolution/format, requested FPS range. NOT observable: frames actually analysed per second** — that must come from the app. Needs a camera-equipped unit to validate.
- `webview`, `network`
- `screencap` — **deferred past v1.** Leave room in the module interface. When built: `adb shell screencap` every 2–5 min gives visual regression, a picture per incident, and direct black-screen detection (a mostly-black framebuffer *is* the freeze, observed rather than inferred).

---

## 5. Metric schema

Naming: `stick_<subsystem>_<unit>`, Prometheus conventions, base units, `_total` for counters.

```
stick_gpu_pages_total{serial}                              70597
stick_gpu_pages{serial,proc="dplayer"}                     68672
stick_gl_mtrack_bytes{serial,pkg="dolphinstore"}           303730688
stick_mem_available_bytes{serial}                          2368237568
stick_cma_free_bytes{serial}                               594620416
stick_temp_celsius{serial,zone="soc"}                      76.2
stick_temp_celsius{serial,zone="ddr"}                      78.6
stick_loadavg{serial,window="1m"}                          6.16
stick_cpu_jiffies_total{serial,cpu="0",mode="user"}        588450
stick_net_bytes_total{serial,iface="wlan0",dir="rx"}       9820117
stick_app_pid{serial,pkg="dolphinstore"}                   1689
stick_proc_uptime_seconds{serial,pkg="dolphinstore"}       2745
stick_heartbeat_age_seconds{serial}                        0.4
stick_vsync_count_total{serial}                            16498
stick_uptime_seconds{serial}                               3208.85
stick_hdmi_connected{serial}                               0

stick_gpu_faults_total{serial,type="DATA_INVALID_FAULT"}   1
stick_gpu_faults_total{serial,type="JOB_READ_FAULT"}       0
stick_gpu_faults_total{serial,type="Job_Hard_Stopped"}     0
stick_log_lines_total{serial,level="E"}                    247
stick_errors_total{serial,sig="unix_domain_socket"}        31
stick_dropbox_entries_total{serial,kind="system_app_crash"} 3
stick_reboots_total{serial,reason="reboot"}                1
stick_unparsed_lines_total{serial}                         12
stick_collector_up{nuc="nuc-a"}                            1
```
~40 series/device → **800 active series** at 20 devices.

**Errors: the text does not go into VictoriaMetrics; the rate does.** Counters answer *how many and when*; Loki/Parquet answer *what did it say*. This is also why dedup loses nothing — the rate lives in the counter.

### 5.1 Cardinality rules — the cloud cost bomb
- **`creative_id` must NOT be a metric label.** With hundreds of creatives × 40 series × 20 devices you reach 400k+ active series.
- **Creative intervals are event rows:** `(start_ts, end_ts, serial, creative_id, campaign_id, url, duration_s, mime, exchange)`. Unlimited cardinality, costs nothing.
- GPU/memory stay **plain per-device series**. Analysis joins the two on timestamp — which is also the *better* question, because you get slope-within-interval rather than a smeared average.
- Safe as labels (low cardinality, verified): `mime` (1 value: `weblink`), `exchangeType` (1 value: `OW_DL`), `zone`, `level`, `type`.

### 5.2 Tagging — derived, not declared
`app_version` (from `vc:`) and `creative_id` come **free from the app's logs**. So passive capture is already comparable across builds. A `run_id` with free-form `k=v` labels (`cache=on`, `inlined=yes`, `fps_target=15`) is added only for what the device cannot know.

**Exempt from all retention, forever:** per-run summary rows — `(run_id, app_version, tags, gpu_slope, gpu_peak, fault_counts_by_type, time_to_first_hard_stop, thermal_max, restart_count, heartbeat_stalls)`. Kilobytes per run, and it's what makes build comparison work beyond any window.

---

## 6. Incident engine (on the NUC)

1. **Candidate:** heartbeat stops advancing > 30 s, or all streams go quiet.
2. **Preserve immediately** — snapshot the rolling 1 Hz buffer with its tags. Cheap, and irreversible if skipped.
3. **Retrospective verdict** on reconnect, into exactly one of four:

| verdict | evidence |
|---|---|
| **network loss** | reconnects, and the on-device ring file shows **no gap** |
| **device freeze** | reconnect failed, and the ring file **stops** at T |
| **reboot** | uptime reset; `sys.boot.reason` explains it |
| **collector fault** | the collector itself restarted — it logs its own lifecycle so it never blames a device for its own downtime |

4. **Forensic pull** after recovery: `getprop sys.boot.reason`, `dumpsys dropbox --print` (`SYSTEM_BOOT`, `SYSTEM_LAST_KMSG`, crashes/ANRs), the ring file, `Dolphin_File.log*`. Bundle to `s3://…/incidents/<id>/`.

**The alert that matters is the run-up, not the freeze.** `Job Hard-Stopped` with no following `Reset complete`, plus a stalled heartbeat, plus GPU pages high against their envelope, is visible **while the device is still reachable**. That's the alert that gets someone to the bench with an HDMI cable. "Went dark 40 minutes ago" cannot.

**`sys.boot.reason` was plain `reboot`, not `reboot,deviceowner`** — the scheduled-vs-failure discriminator needs a wider value vocabulary than the original handover assumed. Collect observed values before hard-coding.

---

## 7. On-device recorder

Push to `/data/local/tmp/rec.sh`, launch detached per §1.12, relaunch on every reconnect (it does not survive reboot).

Requirements:
- **Append-only.** A freeze mid-write costs at most one line.
- **Size-bounded rotation**, not time-bounded. A shell script cannot trim by time cheaply — that means scanning and rewriting, and a rewrite in flight at the freeze instant is exactly when everything is lost. Size rotation is one `mv` plus a fresh append.
- **Rotate at 16 MB, keep 2 files → 16–32 MB retained ≈ 1–2 days** at 184 B/s. Against 24 GB free this is nothing; hold 7+ days if wanted (~112 MB).
- **Self-timestamp every record with `/proc/uptime`.** Cadence is ~1.1 s, not 1.0 s.

**This contradicts the original handover's §6 advice** to keep the hot buffer in RAM and flush to flash only on anomaly. At 16 MB/day (~5.8 GB/year/device) flash wear is a non-issue for eMMC, and **a RAM buffer dies with the freeze** — defeating the recorder's entire purpose. Write to flash continuously.

---

## 8. Dashboards (proposed — refine with the user)

The user has **not used Grafana before** but has used Kibana. Keep panels obvious and few; resist density.

### 8.1 Fleet Overview
- **Status table**, one row per stick: friendly name, online, `heartbeat_age_seconds`, GPU pages (+ sparkline), soc temp, `Job Hard-Stopped` count 24 h, app version, current creative, owning NUC. Colour by threshold.
- **Stat tiles:** devices online / total, devices with faults in 24 h, devices over 70 °C, open incidents.
- **GPU pages, all devices, 24 h** — one line per stick, to spot the outlier.
- **Fault rate by type**, stacked, 24 h.
- **NUC health** — `stick_collector_up` per NUC. The dead-man's switch.

### 8.2 Device Detail (the workhorse — must support the click-through flow)
Template variable `$serial`. Shared time range across every panel.
- **GPU pages + `GL mtrack`** overlaid, with a **rolling max envelope** — the envelope is the leak signal, not the raw sawtooth.
- **Annotations overlaid**: creative changes, WebView swaps, GPU faults, restarts, reboots.
- Memory: MemAvailable, CmaFree, app PSS.
- Thermal: both zones + loadavg.
- Heartbeat age, with the 30 s threshold drawn.
- Display liveness: vsync rate; `stick_hdmi_connected`.
- Network: rx/tx, plus connection state.
- **A Loki log panel at the bottom, time range linked to the graphs above.** This is the whole point: click a spike, read the lines. Default query `{serial="$serial"}`, quick filters for `|= "mali"`, `level="E"`.

### 8.3 Build / Run Comparison
Template variables `$app_version_a`, `$app_version_b` (or `$run_id`).
- **GPU slope per run**, side by side.
- **Time-to-first-`Hard-Stopped`** per run.
- **Fault count by type**, grouped bars.
- **Thermal max / mean** per run.
- **Table of run summaries** — the §5.2 exempt rows.
- **Per-creative GPU slope**, joining creative intervals against GPU series. This is the A/B answer.

### 8.4 Incident Review
- Incident list from SQLite: id, serial, start, verdict, run tags, link to the S3 bundle.
- On selection: the preserved 1 Hz run-up, ±30 min, with dmesg and app logs beside it.

### 8.5 Alert rules (deterministic only — no LLM in v1)
| alert | condition |
|---|---|
| heartbeat stall | `stick_heartbeat_age_seconds > 30` for 1 min |
| GPU hard-stop | any increase in `stick_gpu_faults_total{type="Job_Hard_Stopped"}` |
| GPU envelope climbing | `max_over_time(stick_gpu_pages[1h])` slope positive over 4 h |
| thermal | `stick_temp_celsius > 70` for 5 min |
| unscheduled reboot | `stick_reboots_total` increase with reason ∉ known-scheduled set |
| **NUC down** | `stick_collector_up == 0` for 2 min |
| **parser drift** | `rate(stick_unparsed_lines_total[10m])` above baseline |
| duplicate ingestion | same serial reported by both NUCs |

---

## 9. Build order

Each milestone ends in something demonstrable on real hardware.

**M0 — Foundation.** Terraform: EC2 t4g.medium, 100 GB gp3, S3 bucket + lifecycle, IAM roles, security group. docker-compose: VictoriaMetrics, Loki (S3 backend), Grafana + provisioning. Swap file. SQLite control plane + device registry with NUC ownership. Supervisor: connect → `adb root` → verify debugfs readable → apply + **verify** log remediation (§1.7) → capture `boot_epoch` → spawn streams → backoff/reconnect. A probe script reporting which assumed commands actually work per device.
*Done when:* one device streams to disk across a forced disconnect **and** a real reboot, with zero chatty drops.

**M1 — 1 Hz spine.** Push the recorder, ingest via `tail -F`, parse, write line protocol to VictoriaMetrics.
*Done when:* Grafana shows GPU pages + temp + MemAvailable with no gap across a connection drop.

**M2 — Streams and derived tags.** logcat/dmesg/app-log parsers; event extraction (faults by type, crashes, ANRs, restarts, reboots + boot reason, **per-package attribution**); derive `app_version`, `creative_id`, HDMI status, heartbeat. Loki shipping. Parquet writer with deterministic naming + nightly compaction.
*Done when:* a GPU chart is annotated with creative changes and fault events, and the same data is queryable in DuckDB.

**M3 — Expensive tier, modules, control UI.** `dumpsys` tier with anomaly burst; module toggles per device/run; run start/stop with labels. Thin control surface (no dashboard — Grafana does that), plus a `/health` status page for when cloud is unreachable.
*Done when:* toggling the camera module changes what's collected, and a labelled run tags its samples.

**M4 — Incident engine.** Silence detection, four-way classification, run-up preservation, forensic pull, S3 bundles.
*Done when:* one real incident is preserved with a correct verdict and a readable bundle.

**M5 — Scale + dashboards + alerts.** Both NUCs, all devices. The four dashboards of §8. Alert rules. `vmbackup` nightly.
*Done when:* two builds are soaked and diffed in one view, and killing a NUC fires an alert.

**M6 — Proving workload: the freeze A/B** (§11).
*Done when:* cache vs inlining is isolated, or shown not to be the trigger.

### Explicitly not in v1
- **LLM triage agent.** Deterministic tripwires cover what matters; nondeterminism in an evidence-gathering system is the wrong trade.
- **Adaptive cadence** beyond the anomaly burst.
- **`screencap` / visual regression** — deferred, interface left open.
- **Athena** — one `CREATE EXTERNAL TABLE` away whenever wanted.

---

## 10. The existing code

`/Users/dane/dolphin/logging_pipeline` — 5 Python files, ~130 KB: FastAPI app, threaded `ADBMonitor`, SQLite schema, event detection, dashboard, Ollama analyzer.

**Decision: new repo. Mine for parts, don't extend.** Its collection model is the opposite of what's needed — `subprocess.run` per metric, no streaming, no `adb root`, no dmesg, no GPU metrics. Worth lifting: working adb invocations, the device registry idea, the reconnect loop, the event-detection heuristics in `_check_events` (reboot via uptime-went-backwards, crash via process-gone-then-returned-within-60s).

**Known bugs in it — do not reproduce:**
- Year-less `log_ts` (§1.11) → `cleanup()` wipes the whole log table; analyzer's error-log query always returns empty.
- `_find_adb()` hardcodes Windows `adb.exe` paths.
- `/api/debug` shells `"adb"` instead of the resolved `ADB_CMD`.
- `_adb()` does `cmd.split()`, so no argument may contain a space.
- `_poll_loop` spawns fresh threads every 2 s with `join(timeout=15)`, orphaning threads for slow devices.

A teammate also drafted a plan (InfluxDB + Loki + Grafana on a DigitalOcean droplet). **Nothing was deployed.** Points of agreement worth keeping: DB off the collector host, fixed-cost hosting, tiered polling, dropbox as best crash source, tag by APK version. Points to reject: automatic `adb bugreport` on crash (1–3 min, 5–30 MB, perturbs the device, and **impossible during a freeze** since adb is refused), and `logcat -b crash -d` polling (use `-b all` in the continuous stream).

---

## 11. The freeze A/B (proving workload)

The suspected trigger is a prod change that both (a) **cached** the weather creative and (b) **inlined 6 files → 1 self-contained HTML**. Two confounded variables.

| weather build | cache | expectation |
|---|---|---|
| old multi-file | off | known-good baseline |
| old multi-file | on | isolates caching |
| inlined single-HTML | off | isolates inlining |
| inlined single-HTML | on | = current prod (freezes) |

Compare per cell: **GPU-page slope envelope, GPU-fault rate, time-to-first-`Hard-Stopped`.**

Notes:
- The user handles installs and creative pinning; the rig only observes. But **per-creative attribution is free from the logs** (§1.9), so passive capture already supports the analysis.
- `durationSecs: 5` is the current dwell. If settable to 1–2 s, a multi-hour repro accelerates to ~10–20 min.
- `weather/?version=3` in the URL may itself be the A/B lever.
- **Run these cells with a display attached.** Each is ~20 min, and it keeps the experiment off the critical path of the incident engine being trustworthy yet.

---

## 12. Open questions — ask, don't guess

**Blocking M0:**
1. **Both NUCs:** distro, RAM, disk free, Docker available, Python version, and which subnets each can reach.
2. **AWS:** confirm region (us-east-1, to sit with the existing `devicelogs` API Gateway), and whether credentials are provided or the user applies the Terraform.
3. **Device→NUC split** — assign explicitly, or let the registry allocate.

**Before the schema is frozen:**
4. **How many distinct creatives are in rotation fleet-wide?** Only 2 were seen in 11 minutes. Decides nothing structurally (creative_id is an event row either way) but sizes the events table.
5. **Which identifier is canonical?** adb gives `ro.serialno = D-005-02408`; the app reports `deviceId = DD-H33CBP`, plus `venueCode` and `installationId = 63718089`. Analysis needs one join key and a mapping table.
6. **What is behind `https://tvaqma11rf.execute-api.us-east-1.amazonaws.com/Prod/devicelogs`?** If reachable, it may already hold app-side history and is the natural join target.

**Lower stakes:**
7. A **camera-equipped unit** to validate the camera module.
8. **Alert delivery** — email, Slack, who receives.
9. **Is `durationSecs` settable?** (§11)
10. **Can the DPlayer devs reduce log verbosity at source?** One compact line instead of a pretty-printed object removes **175 of the 238 MB/day** on device and eliminates chatty pressure entirely. Cheapest possible fix, and independent of this rig.
11. **The thermal runaway** (§1.6) — 60 → 78 °C in 50 min, sustained. Probably worth looking at before building anything.
