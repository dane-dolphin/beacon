# TODO

Carried out of the 2026-07-28 hardware-validation session.
Context and rationale: `NOTES-2026-07-28.md`.

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
