# TODO

Carried out of the 2026-07-28 hardware-validation session.
Context and rationale: `NOTES-2026-07-28.md`.

---

## Added 2026-07-29 — logging gaps for the fleet dashboard

Target is parity with the earlier `DD Monitor` per-device cards (CPU %, RAM,
Storage %, per-app CPU/RAM, top-N process breakdown, net throughput, ping).

**Already collected** — CPU % (derivable from the per-core `user,sys,idle`
jiffies in field 10), temps, net ↓/↑ (delta of the rx/tx counters), app
running/stopped (presence of `app_pid`).

- [ ] **P1 — `rec.sh` v2: per-app CPU + RAM.** Today `rec.sh` tracks exactly one
      pid (the `PKG` argument) and only its *GPU pages* — there is no per-process
      CPU or RSS anywhere. Add a block for a configured package list:
      `apps:name,pid,rss_kb,utime,stime;...`
      Sources: `/proc/<pid>/status` → `VmRSS`, `/proc/<pid>/stat` fields 14/15
      (utime/stime). Measured on D-005-02408: the app is **703 MB / 143% CPU**
      and its WebView renderer another **188 MB / 80%**, which is what the ~5.9
      loadavg is made of — none of that is visible today.
      Note `io.shoonya.shoonyadpc` (~178 MB) is the closest match to the
      dashboard's `BAGENT`; there is no process literally named `bagent`.

- [ ] **P1 — `rec.sh` v2: storage.** Add `disk:total_kb,avail_kb`. Careful:
      plain `df /data` on these devices resolves to an obb bind mount
      (`/mnt/installer/0/emulated/0/Android/obb`), so target the real
      filesystem, not whatever `/data` maps to.

- [ ] **P1 — capture `MemTotal` once.** The dashboard shows RAM *used*, but
      `rec.sh` records only `MemAvailable`. `MemTotal` is constant per device
      (3840484 kB on D-005-*, 3767328 kB on DD-002-0939) — put it in the
      `#beacon-rec` header rather than paying for it every second.

- [ ] **P2 — top-N processes, on a SEPARATE low-frequency line.** Emit a
      `#top` line every 15–30 s from `ps -A -o RSS,PCPU,NAME --sort=-RSS`.
      Three constraints, all real:
      1. Do **not** walk `/proc` at 1 Hz — too expensive on this SoC.
      2. `rec.sh` is the process that **wedged during the 07-29 freeze** (it
         blocked reading mali debugfs and died 0.9 s before the kernel's first
         fault). Every syscall added to the 1 Hz loop is another place it can
         block; keep the hot tier lean and put expensive work where a stall
         cannot kill the cheap tier.
      3. Cap N at ~5 and normalise process names — a per-process `proc` label
         is the same cardinality trap already flagged for `sig` below.

- [ ] **P2 — parser + tests for rec.sh v2.** `parse_recorder_line` hard-requires
      `len(parts) == 11` and returns `None` otherwise, so a v2 line is silently
      dropped. Version the header, accept both, and add regression tests for
      each — existing captures in `var/spool` must keep parsing.

**Explicitly NOT worth adding:** more app/logcat logging. The 07-29 freeze was
diagnosed entirely from data already on disk, and it was not memory-related
(zero `OUT_OF_MEMORY`/alloc-failure lines; `MemAvailable` flat at ~2.29 GB
through the freeze). Adding app-side logging would not have caught it.

## Added 2026-07-30 — remove unwanted logs (§3.4 denylist)

**~80% of logcat is removable noise on every stick.** Measured 2026-07-30 over a
60,000-line sample of the most recent hour per device. The signal we actually
use is ~2.5%. Ranked by savings:

| share | class | pattern |
|---|---|---|
| **67.4 / 67.5 / 45.1%** | **RN ad-object dump** | `ReactNativeJS:` pretty-printed object fields — `height:`, `width:`, `durationSecs:`, `mime:`, `campaignId:`, `exchangeType:`, `originalAd:`, `{`/`}`/`]` |
| **25.5%** (DD-Stick only) | **USB/adbd churn** | `udc-core: couldn't find an available UDC`, `timed out while waiting for FUNCTIONFS_BIND`, `UsbFfs: *`, `read descriptors`, `read strings` |
| 6.1 / 6.1 / 4.1% | RN SyncMgr/Scheduler chatter | `ReactNativeJS.*(SyncMgr|Scheduler|AdsProvider)` |
| 4.1% (DD-Stick) | Realtek BT heartbeat | `rtk_heartbeat:` |
| 3.1% | RIL modem probing | `RILC`/`RILU`/`find_pci_device` — there is no modem in these devices |
| 1.5% | ThermalService spam | `ThermalService: CPU temperatures:` — we already sample temps at 1 Hz in rec.sh |
| 1.5% | Unix domain socket EPERM | `failed to create Unix domain socket: Operation not permitted` |
| 0.4% | chromium first_paint | `Invalid first_paint` (already noted in P3) |
| 0.3% | SELinux avc denials | `avc: denied` — SELinux is permissive on these builds, so these are informational |

- [ ] **P1 — kill the RN ad-object dump.** Single biggest win by far: two thirds
      of all logcat. The app pretty-prints the entire ad object across ~20 lines
      on every creative cycle, every ~5 s, forever. The URI is *already* in the
      `Setting Prefetch Source` line, so the dump is pure duplication.
      Best fixed in the app; failing that, denylist the field-line shapes.

- [ ] **P1 — fix the DD-002-0939 USB/adbd retry loop at the source.** 25.5% of
      its logcat *and* 98.7% of its dmesg. `adbd` is retrying USB gadget mode
      forever (`FUNCTIONFS_BIND` timeout → `UsbFfs` teardown → repeat) because
      adb is on TCP and no USB host is attached. This is a root-cause fix, not a
      filter: stop advertising the USB adb function. It also crowds real kernel
      messages out of the 4 MiB kernel ring — including the mali reset lines the
      freeze rule depends on, which makes this a *correctness* issue, not just
      storage.

- [ ] **P2 — decide WHERE filtering happens.** Three points, different
      trade-offs:
      1. **On-device** (`logcat -P` pid allowlist, or fix the app) — cheapest;
         saves radio, flash and CPU as well as our storage.
      2. **Collector, before spool** — biggest storage win here, since the spool
         is the ~28 MB/h/device problem in P1 below. But it conflicts with
         plan.md's "Parquet is everything, unfiltered, forever" tier.
      3. **Ship-time, before Loki only** — safest; keeps Parquet complete and
         only de-noises what is queried. Recommended default.
      At ~80% reduction the spool figure drops from ~28 MB/h/device to
      ~5.6 MB/h — roughly 560 → 112 MB/h at the 20-stick target.

- [ ] **P2 — DO NOT DROP THESE.** The freeze investigation depends entirely on
      them and they are a rounding error in volume:
      - `mali `, `GPU fault`, `Resetting GPU`, `Reset complete`,
        `Unhandled Page fault`, `JOB_READ_FAULT`, `Job Hard-Stopped` — **0.04%**
      - `Setting Prefetch Source`, `Ad going to be displayed`,
        `Setting Active view`, `Setting PrefetchView` — **2.4%**; these are what
        made per-creative fault attribution possible
      - `context lost` / `Context reset`, `Davey!` (jank), `Process ... died`
      Write the denylist as *exclusions with an explicit keep-list checked
      first*, so a broad pattern can never silently swallow a signal line.

- [ ] **P3 — re-measure after landing.** Re-run the same 60k-line census per
      device and record the new noise share here, so the next person can tell
      whether a regression in app logging has crept back in.

## Added 2026-07-29 — detection and recovery

- [ ] **P1 — an unreachable device logs NOTHING.** `supervisor._connect_cycle`
      returns `False` at `supervisor.py:65-66` with no log line, and
      `Adb.connect` logs its failure at `log.debug` (`adb.py:67`). D-005-02408
      was frozen and unreachable for **13 hours** on 2026-07-29 and `beacon.log`
      recorded not one word about it — the outage was invisible until someone
      looked. Log a WARNING with consecutive-failure count and elapsed downtime,
      and lift the `adb.py` failure off DEBUG. Same class as the
      `VMWriter._post` item in P3.

- [ ] **P1 — incident rule: `Resetting GPU` with no `Reset complete`.** Exact
      device strings (note the SPACE — it is *not* `Reset_complete`):
      ```
      mali fe400000.bifrost: Resetting GPU (allowing up to 500 ms)
      mali fe400000.bifrost: Failed to soft-reset GPU (timed out after 500 ms), now attempting a hard reset
      mali fe400000.bifrost: Reset complete
      ```
      A healthy reset emits all three. A fatal one stops after the first (plus
      its register dump). Alert when `Reset complete` does not follow
      `Resetting GPU` within ~2 s.
      **The arithmetic confirms this exactly** (measured 2026-07-30):
      D-005-02408 had **21** `Resetting GPU` and **19** `Reset complete` — the
      2 missing land precisely on the two freezes (07-29 00:17:55 and
      07-29 19:45:40). D-005-01860 had **19 and 19** — zero unfinished resets,
      zero freezes. Reset completion is the whole difference between a 200 ms
      hiccup and a 13-hour outage.
      Metric-tier alerting **cannot** catch this: the last 12 rec.sh samples
      before the freeze were entirely normal (GPU 74–79k pages, memavail flat,
      79–80 °C, load ~5.9) and it went normal→dead in under 1.1 s.

- [ ] **P1 — D-005-01860 is at the SAME risk; 02408 is not special.** Both
      Dolphin Sticks fault heavily (1123 vs 780 GPU faults, 21 vs 19 resets).
      The only difference so far is that every one of 01860's resets happened
      to complete. Each reset is effectively a dice roll — do not treat 01860
      as a healthy control, treat it as a stick that has not lost yet.

- [ ] **P2 — DD-002-0939 (Android 14) shows ZERO GPU faults — but the control
      is confounded.** 0 faults / 0 resets over 160k dmesg lines, vs 1123 and
      780 on the Dolphin Sticks. Tempting to conclude the Android 14 / newer
      mali stack is immune — but its **playlist mix is different**: it prefetches
      the portrait creative only **355 times (6.5%)** versus ~4,500 (49.9%) on
      both Dolphin Sticks, i.e. 13× less exposure to the trigger. At the
      measured 3.6% fault rate 355 prefetches would still predict ~13 faults
      and we saw 0 (p≈3e-6), so it is suggestive — but platform and exposure
      are not separable from this data. **Clean test: equalise the playlist**
      (give DD-002-0939 the same ~50/50 mix) and see whether faults appear.

- [ ] **P1 — do NOT cold power-cycle a frozen stick; ramoops is live.** Bootargs
      carry `ramoops.pstore_en=1 record_size=0x8000 console_size=0x4000` and
      dmesg confirms `pstore: Registered ramoops as persistent store backend`
      (1 MB @ 0x07400000). `/sys/fs/pstore` was empty on 07-29 **only because
      the freeze was cleared with a cold power pull, which wipes DRAM**. A warm
      reset preserves it. Consider raising `console_size` above its current
      16 KB.

- [ ] **P2 — why did the hardware watchdog not fire?** The device sat frozen
      13 h without resetting. If the watchdog can be enabled, a freeze becomes a
      ~30 s warm reboot that *also* preserves the ramoops crash log — recovery
      and forensics in one change. Higher leverage than root-causing the driver.

## Added 2026-07-29 — DD-Stick (Android 14) platform support

`DD-002-0939` @ 192.168.0.107 is a **different platform** from the D-005-*
sticks: Google `DD-Stick`/`ross`, Android **14** (vs Droidlogic `ohm`,
Android 11). Same amlogic SoC family. Probe 13/14, now streaming.

- [x] **`logd --reinit` ordering bug — FIXED 2026-07-29.**
      `apply_log_remediation` ran `logcat -b all -G 4M` and *then*
      `logd --reinit`, which re-initialises the buffers and undid the resize.
      On Android 11 `--reinit` re-reads `persist.logd.size` and lands on 4M, so
      the bug was invisible; on Android 14 it reset all four buffers to the
      256 KiB default. Every freshly-added device was silently left with 256 KiB
      buffers. Fixed by swapping the order (reinit, then `-G`). Verified 4 MiB
      stable on DD-002-0939 and no regression on both Dolphin Sticks.
      **Still needed: a regression test**, since nothing in the suite covers it.

- [ ] **P1 — DD-002-0939 floods dmesg: 158k of its 160k lines are USB-gadget
      spam.** `udc-core: couldn't find an available UDC or it's busy` ×104,809,
      plus `read descriptors` / `read strings` ×26,592 each. That is ~98.7% of
      its kernel log. Two consequences: it burns spool/Parquet storage at a
      large multiple of the other devices, and it crowds real kernel messages
      out of the 4 MiB kernel ring — including exactly the mali reset lines the
      freeze rule depends on. Root-cause the failing gadget bind (likely
      something repeatedly trying to start USB gadget mode while adb is on TCP),
      or denylist the pattern per §3.4.

- [ ] **P2 — `thermal_ddr` does not exist on DD-Stick.** It has only
      `thermal_zone0` (`soc_thermal`); there is no `ddr_thermal`. `rec.sh`
      writes an empty field and the parser maps it to `None`, so it degrades
      gracefully — but `beacon probe` reports 13/14 and will keep looking like
      a partial failure. Make the thermal-zone set discovered per platform
      rather than hard-coded to zone0/zone1. It also runs far cooler:
      **57–65 °C vs 76–82 °C** on the Dolphin Sticks.

- [ ] **P3 — per-device `app_package` matters more now.** Already listed in P2
      below for the TV box; with a third platform in the fleet the global
      `app_package` on `Config` is increasingly wrong.

---

## P1 — do first

- [ ] **Spool pruning.** `parquet_ship.process_closed_hours()` writes the
      `.jsonl.done` marker but never deletes the `.jsonl`, so every hour is
      stored twice — raw JSONL plus a ~12× smaller Parquet copy of identical
      data (`var/spool` 185 MB vs `var/parquet` 15 MB). Costs **~28 MB/h per
      device** (~20 GB/month/device; ~400 GB/month at the 20-stick target).
      Fix is one line after a successful convert: delete the `.jsonl`, keep
      the `.done` marker so nothing is reprocessed. Safe because Parquet is
      the designed "everything, unfiltered, forever" tier and the spool's job
      is *unshipped* batches only.
      **Deliberately deferred** — it is the only code path that deletes data,
      so land it with someone watching, not unattended.

- [ ] **Explain the doubled sample rate in 22:04–23:35 on 2026-07-28.**
      That window has ~2× the expected points (1090 vs 544 samples/10 min for
      a single 1 Hz series). Ruled out: duplicate `rec.sh` instances (exactly
      one per device, pidfiles match). Leading theory: each of the 739
      reconnect cycles re-replayed part of `rec.log`, and a slightly different
      `boot_epoch` per cycle landed near-duplicate points at offset
      timestamps. Values are correct so graphs read fine; it inflates storage
      and would corrupt any rate() maths over that window.
      Look at `supervisor._establish_boot` + `registry.record_boot` —
      specifically whether `boot_epoch` is pinned once per boot or recomputed.

## P2

- [ ] **`rec.sh` mali parser for rk3588**, to turn GPU collection on for the
      TV box. Its `/sys/kernel/debug/mali0/gpu_memory` is 2-column
      (`mali0  23881` / `kctx-0x…  4007`) versus the Dolphin Stick's
      `mali0 total used_pages N` / `kctx pid pages`. Current parser yields an
      empty total and cannot attribute pages to a pid, so `modules: []` for
      that device. Note: editing `rec.sh` changes its md5 and therefore
      restarts the recorder on **both** devices.

- [ ] **Per-device `app_package`.** It is global on `Config` today, so the TV
      box tails a Dolphin app-log path that does not exist and logs
      `app log tail ended (EOF) — reopening` every 5 s. Harmless but noisy.

- [ ] **Multi-hour clean run to settle the GPU-leak question.** The plateau
      evidence is solid but only covers ~100 min of previous-boot data; it
      rules out the +30 MiB/h ramp rate, not a slow multi-hour leak.

## P3

- [ ] **`stick_errors_total` cardinality** — 513 series from the per-signature
      `sig` label on ONE device. Same risk class §5.1 flags for `creative_id`.
      Check before scaling to 10–20 sticks.

- [ ] **Loki rejects backfill older than ~45 min** (`entry too far behind`),
      so text logs recovered after a long outage are not queryable even though
      they are safe in Parquet. Consider raising the ingester's out-of-order
      window.

- [ ] **Denylist candidates for §3.4** — measured on real traffic:
      `chromium … Invalid first_paint` is 210 of 238 applog errors, and the
      GMS `FATAL EXCEPTION` loop fires in bursts every ~15 s. Neither is ours.

- [ ] **Promote `VMWriter._post` failure logging from DEBUG to WARNING.**
      A broken metrics path currently shows up only as missing data, never as
      an error in the run log — this cost real debugging time this session.

- [ ] **Commit the repo.** Everything is staged but there are still zero
      commits on `main`.
