# beacon — Dolphin Stick lab telemetry platform

A lab canary rig for 10–20 rooted Dolphin Sticks: regression detection across
app builds, live health + alerting, forensic reconstruction, and capacity
testing — one collection layer, three storage tiers.

**`plan.md` is the authoritative design document** (verified device facts,
architecture, rationale). Code comments reference its sections as `§n.n`.

```
sticks (adb TCP :5555) ──> NUC collector (this repo, Python)
                              ├─ VictoriaMetrics  1 Hz numeric series, 1 yr
                              ├─ Loki             filtered text, 30 d
                              └─ Parquet (S3)     everything, unfiltered, forever
                                        └─ Grafana dashboards on top
```

## Layout

| path | what |
|---|---|
| `plan.md` | the build brief — ground truth |
| `DEPLOY.md` | deployment runbook: NUC all-in-one, then EC2 split |
| `collector/` | Python package `beacon_collector` (NUC side) |
| `device/rec.sh` | on-device 1 Hz recorder, survives adb disconnects (§1.12) |
| `deploy/` | docker compose: VictoriaMetrics + Loki + Grafana + provisioning |
| `deploy/systemd/` | collector + console units — copy, do not retype |
| `infra/` | AWS SAM template (EC2 + S3 + IAM), `sam validate --lint` clean |
| `config/beacon.yaml` | per-NUC config: endpoints, device→NUC ownership |
| `scripts/` | `check.sh` health check, fixture capture, e2e, compaction, deploy bundle |

## Quick start (local dev — this machine is the NUC)

```bash
# 1. observability stack
cd deploy && docker compose -f docker-compose.yml -f docker-compose.local.yml up -d

# 2. python env  (Pop!_OS/Ubuntu: `sudo apt install -y python3-venv` first,
#                 or the venv fails at ensurepip)
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# 3. tests + offline end-to-end (replays measured fixtures through the stack)
.venv/bin/pytest -q
.venv/bin/python scripts/e2e_local.py

# 4. against the real device (192.168.0.106:5555 reachable)
.venv/bin/beacon probe D-005-02408     # which assumed commands work
.venv/bin/beacon onboard D-005-02408   # log-buffer remediation §1.7, verified
.venv/bin/beacon run                   # supervise + stream all owned devices
```

Grafana: <http://localhost:3000> (admin / beacon-dev) → Beacon → Device Detail.

## Cloud deploy (when ready)

```bash
cd infra
sam validate --lint
sam deploy --guided --parameter-overrides AllowedCidr=<site-ip>/32 KeyName=<keypair>
scripts/push_deploy_bundle.sh <bucket-from-output>   # then reboot instance or compose up via SSM
```

Point `config/beacon.yaml` endpoints at the stack outputs (EIP), set
`s3_bucket`, and the same collector ships to the cloud.

## Ad-hoc SQL over Parquet

```sql
-- duckdb
SELECT dt, level, tag, count(*)
FROM read_parquet('var/parquet/logs/*/*/*.parquet', hive_partitioning=1)
GROUP BY ALL ORDER BY 4 DESC;
```

Nightly: `scripts/compact_parquet.py var/parquet` merges hour objects into
day files (§3.6). Athena later = one `CREATE EXTERNAL TABLE` over the same
S3 prefixes.

## Non-negotiables (from plan.md — do not regress these)

- Never store a year-less timestamp; collector receive-time is primary (§1.11).
- `creative_id` is an event row, never a metric label (§5.1).
- Filtering is a denylist with 1:100 sampling; unparsed lines always ship;
  Parquet gets everything regardless (§3.4).
- Never average GPU pages when rolling up — envelope is the signal (§1.4).
- The on-device recorder writes to flash, append-only, size-rotated (§7).
