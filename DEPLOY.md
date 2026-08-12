# Deploying beacon

Runbook for getting beacon off the dev laptop and onto real hardware.
`plan.md` is the design authority; this file is the sequence of commands and
the traps between them.

Two phases, in order. **Phase 1 is the current plan** — everything local on
hyperion, no AWS. It stands alone and can run indefinitely. Phase 2 is a later
endpoint change, not a rebuild.

| phase | collector | VictoriaMetrics + Loki + Grafana | Parquet |
|---|---|---|---|
| **1 — all-in-one (now)** | hyperion | hyperion (docker) | hyperion disk |
| 2 — split (later) | hyperion | EC2 (docker) | S3 |

Each phase has two halves, and they are independent of each other:

- **the collector** — [Phase 1, steps 1–8](#phase-1--hyperion-all-in-one)
- **the dashboards** — [Dashboards](#dashboards)

Phase 2 is three lines of `config/beacon.yaml` plus an IAM gap that is not yet
built. See [Known gaps](#known-gaps--read-before-phase-2) before starting it.

---

## What you are actually deploying

Two artifacts with nothing in common but a config file.

**The collector** is a single long-lived Python process, `beacon run`. It is
**not** containerised — there is no Dockerfile in this repo. It listens on
nothing; every connection is outbound (adb TCP to the sticks, HTTP to
VictoriaMetrics/Loki, boto3 to S3). It runs under systemd.

**The stack** is three containers from `deploy/docker-compose.yml`:
VictoriaMetrics, Loki, Grafana.

**The collector must sit on the sticks' LAN.** adb has no route to a device
behind NAT (`plan.md` §2.1). This is why EC2 can never collect, and why
Phase 2 moves only the storage half to the cloud.

---

## Phase 1 — hyperion, all-in-one

Target: the i5 / 8 GB Linux box. RAM fits — ~1.1 GB stack plus ~1.0 GB
collector anon RSS of 8 GB.

### Confirm before starting

- [ ] hyperion is on the sticks' subnet (`192.168.0.0/24`) — not routed, *on* it
- [ ] Python ≥ 3.10 (`python3 -V`; Pop!_OS 24.04 ships 3.12.3 — fine)
- [ ] disk headroom: the spool ran to 27 GB on the laptop
- [ ] you can reach a stick: `adb connect 192.168.0.100:5555`

### 1. Prerequisites

hyperion runs **Pop!_OS**, so this is `apt`. Pop is Ubuntu-derived
(`ID_LIKE="ubuntu debian"`), and everything below is plain Ubuntu practice with
one exception, flagged inline.

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg unzip git \
                    python3-venv python3-pip
```

`python3-venv` is not optional. Without it `python3 -m venv` fails at the
`ensurepip` step — it is a separate package on Debian/Ubuntu, and Pop!_OS 24.04
does not ship it by default.

**Docker.** Use Docker's own apt repository, not `apt install docker.io`: the
compose files here use `docker compose` (the v2 plugin), which the distro
`docker.io` package does not provide.

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# THE POP!_OS GOTCHA: Docker's published instructions interpolate $ID, which is
# "pop" here, and download.docker.com has no pop/ path — it 404s. Pin the repo
# to ubuntu and use $UBUNTU_CODENAME (noble on Pop 24.04), which Pop sets for
# exactly this purpose.
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "$UBUNTU_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Group membership only applies to a **new login session** — log out and back in,
or `newgrp docker` in the current shell, or every command below fails with
permission denied on the socket.

Now verify, and note *what* each check proves:

```bash
docker compose version    # only the CLI plugin — passes even if the daemon is dead
docker info               # actually talks to the daemon
docker run --rm hello-world
```

**If `docker.service` failed to start**, and especially if the install output
said `Stopping 'docker.service', but its triggering units are still active:
docker.socket` or `Could not execute systemctl`, you upgraded over the distro
packages — apt will have removed `docker.io`, `containerd` and `runc`. That
transition leaves stale units and sometimes a config the new daemon rejects.
Diagnose containerd first; if it is down, Docker cannot start regardless and
its own journal will be misleading:

```bash
systemctl status containerd --no-pager -l
systemctl status docker.service --no-pager -l
journalctl -xeu docker.service --no-pager | tail -40
```

```bash
# 1. stale units left by the package swap
sudo systemctl daemon-reload && sudo systemctl reset-failed
sudo systemctl enable --now containerd
sudo systemctl enable --now docker.socket
sudo systemctl enable --now docker

# 2. containerd config written for the old version — 2.x refuses to parse it
sudo mv /etc/containerd/config.toml /etc/containerd/config.toml.bak
sudo systemctl restart containerd && sudo systemctl restart docker

# 3. leftover /etc/docker/daemon.json from docker.io — an unknown key makes the
#    daemon exit immediately; move it aside and restart
```

Installing Docker CE on a box that never had `docker.io` avoids all of this.

**Android platform-tools** — `adb` is not expected on PATH; the systemd unit
points `BEACON_ADB` at it.

```bash
sudo mkdir -p /opt/android && cd /opt/android
sudo curl -fsSLO https://dl.google.com/android/repository/platform-tools-latest-linux.zip
sudo unzip -q platform-tools-latest-linux.zip && sudo rm platform-tools-latest-linux.zip
/opt/android/platform-tools/adb version
```

### 2. Repo and Python environment

Get the repo to `/opt/beacon` by whichever route works. **Verify the remote
actually has your commits first** — `ssh -T git@github.com` and `git fetch
origin` both have to succeed on the dev machine, or a clone gives you a stale
tree or nothing at all:

```bash
sudo git clone <repo-url> /opt/beacon
```

If the remote is not set up, copy from the dev machine instead. The repo minus
build artifacts is ~2 MB and `.git` comes along, so history stays intact and
you can push from either box later:

```bash
# run on the dev machine. Use hyperion's IP, NOT its hostname: both boxes are
# Pop!_OS and default to the hostname "pop-os", so a name-based target
# resolves to the sending machine and silently does nothing useful.
rsync -av --exclude '.venv' --exclude 'var' \
  ~/Work/beacon/ hyperion@<hyperion-ip>:/tmp/beacon-src/

# then on hyperion
sudo mv /tmp/beacon-src /opt/beacon
```

Never copy `.venv/` (~250 MB, built against the other machine's Python) or
`var/` (~4 GB). hyperion must start with an empty `var/`: a borrowed
`registry.sqlite3` carries another machine's boot epochs, and `boot_epoch` is
what converts every device-monotonic timestamp to wall clock (§1.11).

```bash
sudo chown -R "$USER" /opt/beacon
cd /opt/beacon
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'     # add '[s3]' only in Phase 2
```

`var/` is gitignored, so nothing carries over from the laptop. That is
intended — see *What does not migrate* below.

### 3. Observability stack

```bash
cd /opt/beacon/deploy
export GRAFANA_ADMIN_PASSWORD='<something-not-beacon-dev>'
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

The `.local` override is what puts Loki chunks on the filesystem instead of
S3. Without it Loki will try to reach a bucket that does not exist yet.

### 4. Configuration

Edit `config/beacon.yaml`. Three changes, and the second one is the one people
miss:

```yaml
nuc_id: nuc-hyperion          # was nuc-dev

devices:
  D-005-02408:
    nuc: nuc-hyperion         # <- EVERY device, or this box collects nothing
```

`my_devices()` returns only devices whose `nuc:` equals `nuc_id`. Change one
and not the other and the collector starts cleanly, logs nothing unusual, and
collects zero devices.

Also make `paths:` absolute — they are relative to the repo root today, which
breaks under a systemd unit whose WorkingDirectory you may change:

```yaml
paths:
  registry_db: /var/lib/beacon/registry.sqlite3
  spool_dir: /var/lib/beacon/spool
  parquet_dir: /var/lib/beacon/parquet
```

```bash
sudo mkdir -p /var/lib/beacon /var/log/beacon
sudo chown -R "$USER" /var/lib/beacon /var/log/beacon
```

`endpoints:` stay on `localhost` in Phase 1.

### 5. systemd unit

**The unit is not in the repo.** It exists only as a *user* unit on the dev
laptop. On a real box use a system unit — write
`/etc/systemd/system/beacon-collector.service`:

```ini
[Unit]
Description=Beacon collector — streams Dolphin Sticks to VM/Loki/Parquet
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=beacon
WorkingDirectory=/opt/beacon
Environment=BEACON_ADB=/opt/android/platform-tools/adb
ExecStart=/opt/beacon/.venv/bin/beacon run
Restart=always
RestartSec=10
StandardOutput=append:/var/log/beacon/beacon.log
StandardError=append:/var/log/beacon/beacon.log

[Install]
WantedBy=multi-user.target
```

### 6. Cutover

Order matters. **Two collectors on one device means duplicate ingestion** —
doubled stick load, doubled ingest, and a metrics history that silently
disagrees with itself.

```bash
# 1. STOP the laptop first
systemctl --user stop beacon-collector && systemctl --user disable beacon-collector

# 2. prove hyperion can talk to the hardware
cd /opt/beacon
.venv/bin/beacon probe D-005-02408      # expect 14/14
.venv/bin/beacon onboard D-005-02408    # log-buffer remediation, all four at 4 MiB

# 3. start
sudo systemctl daemon-reload
sudo systemctl enable --now beacon-collector
sudo systemctl status beacon-collector
```

### 7. Verify

```bash
.venv/bin/pytest -q                     # 41 tests
.venv/bin/python scripts/e2e_local.py   # replays fixtures through VM + Loki
tail -f /var/log/beacon/beacon.log
```

Then Grafana at `http://<hyperion>:3000` → Beacon → **Device Detail**. Data
should appear within ~15 s.

Two things to check that are easy to miss:

- **VictoriaMetrics write failures are logged at DEBUG.** A broken metrics
  path shows up only as missing data, never as an error. If graphs are empty
  but the log looks fine, run the collector with `-v` and look for
  `VM write failed`.
- **A stuck reconnect loop has no ERROR lines either.** Its signature is
  `streaming (boot_epoch=…)` / `stream …-logcat ended cleanly (EOF)` /
  `session ended; will reconnect` repeating every few seconds.

### 8. Scheduled maintenance

```cron
# nightly Parquet compaction (§3.6) — merges hour objects into day files
15 3 * * *  /opt/beacon/.venv/bin/python /opt/beacon/scripts/compact_parquet.py /var/lib/beacon/parquet
```

**Spool pruning is deliberately not on a cron yet.** The collector prunes on
its own cycle, but the first *real* delete is to be supervised: run
`beacon prune --dry-run`, read the numbers, and only then let it delete.
Deletion additionally requires the Parquet object to exist with a matching row
count, so a mismatch keeps the file and logs a WARNING.

---

## Dashboards

Grafana at **`http://<hyperion>:3000`**, user `admin`, password from
`GRAFANA_ADMIN_PASSWORD` (defaults to `beacon-dev` if unset — do not leave it
there on a box other people can reach). Dashboards live in the **Beacon**
folder.

Nothing is clicked into place. Both datasources and dashboards are
**provisioned from files** in the repo and mounted into the container, so a
fresh Grafana comes up complete:

| file | becomes |
|---|---|
| `deploy/grafana/provisioning/datasources/beacon.yaml` | the VictoriaMetrics and Loki datasources |
| `deploy/grafana/provisioning/dashboards/beacon.yaml` | the provider that watches the folder |
| `deploy/grafana/dashboards/*.json` | one dashboard each |

### What ships today

**Device Detail** (`beacon-device-detail`) — one device at a time, picked with
the `$serial` variable at the top.

- GPU pages (raw 1 Hz + 30 m rolling max envelope), thermal zones, memory,
  heartbeat age, load average, network, vsync/HDMI liveness
- RAM and CPU by process, top 5, stacked
- Error rate (E/F including deduplicated), log lines by level, errors and
  unparsed lines in the last 24 h, top error signatures
- A Loki logs panel with source / level / free-text filters

**Process Resources** (`beacon-process-detail`) — the per-process view in
depth, with `$serial` and a `$proc` multi-select.

- RAM (PSS) and CPU stacked by process
- Peak RAM and peak CPU **over the selected range** — instant `topk` over
  `max_over_time`, which is how you answer "who was biggest this week" without
  a range `topk()` flapping series in and out
- System memory vs. the top 5 — a widening gap means growth is *not* in the
  watched processes
- Distinct process-name counts, per device and fleet-wide, as a cardinality
  watch

### Adding or editing a dashboard

Drop a `.json` file into `deploy/grafana/dashboards/`. The provider polls every
**30 seconds** (`updateIntervalSeconds: 30`) — no restart, no import step.

```bash
cp mydash.json /opt/beacon/deploy/grafana/dashboards/
sleep 35
curl -s -u admin:"$GRAFANA_ADMIN_PASSWORD" \
  "http://localhost:3000/api/search?query=" | python3 -m json.tool
```

Give each file a stable `uid` — that is what Grafana keys on, and what links
and bookmarks point at.

**The gotcha:** `allowUiUpdates: true`, so Grafana *lets* you edit and save in
the browser, and the change sticks in Grafana's own database. But
`deploy/grafana/dashboards` is mounted read-only, so nothing is written back to
the file. The next time that file changes on disk, provisioning re-applies it
and your UI edit is gone — and it was never in git in the first place.

Use the UI to experiment, then make it permanent: **Dashboard settings → JSON
Model** (or *Export → Save to file*), and write that over the file in the repo.

### When the dashboards move to AWS

There are two different endgames here and they differ by one file:

- **Grafana moves to EC2 too** (what `infra/template.yaml` does — the same
  compose file runs there). `provisioning/datasources/beacon.yaml` needs **no
  change**: it addresses `http://victoriametrics:8428` and `http://loki:3100`,
  which are docker-compose service names and resolve identically on either
  host.
- **Grafana stays on hyperion**, reading storage in the cloud. Then the
  datasource URLs must become the elastic IP — `http://<EIP>:8428` and
  `http://<EIP>:3100` — and the security group's `AllowedCidr` has to cover
  hyperion's public address.

Either way the dashboard JSON is untouched: panels reference datasources by
`uid` (`beacon-vm`, `beacon-loki`), never by URL.

---

## Phase 2 — EC2 storage, hyperion collector-only

Do Phase 1 first and let it run. Phase 2 changes where three sinks point; it
does not change how collection works.

### 1. Deploy the stack

The instance is **Amazon Linux 2023 on arm64** — the template resolves
`/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64` from SSM,
so the AMI id is never hardcoded and never goes stale. Note this is **Graviton**:
the `InstanceType` choices (`t4g.*`, `m7g.*`) are all ARM, and user-data pulls
the `aarch64` compose binary to match. Switching to an x86 instance type means
changing the AMI parameter and that `ARCH=aarch64` line together.

Amazon Linux uses `dnf`, so the `dnf install -y docker` inside the template's
user-data is correct as written and is unrelated to hyperion's `apt` — the two
machines run different distributions on different architectures on purpose.

```bash
cd /opt/beacon/infra
sam validate --lint
sam deploy --guided \
  --parameter-overrides AllowedCidr=<site-public-ip>/32 KeyName=<keypair>
```

Note the outputs: `PublicIp`, `BucketName`, `GrafanaUrl`.

### 2. Push the bundle — there is an ordering trap

User-data pulls `s3://<bucket>/bootstrap/deploy.tar.gz` on first boot, but the
bucket is created by the *same* stack, so it is empty at that moment. The
template handles this by printing the manual command instead of failing. So:

```bash
scripts/push_deploy_bundle.sh <BucketName-from-output>
# then, on the instance (SSH or SSM Session Manager):
cd /opt/beacon/deploy && docker compose up -d
```

A reboot works too — user-data re-runs and finds the bundle.

### 3. Flip hyperion

```yaml
endpoints:
  victoriametrics: http://<EIP>:8428
  loki: http://<EIP>:3100
  s3_bucket: beacon-telemetry-<account>-us-east-1
  s3_region: us-east-1
```

```bash
cd /opt/beacon
.venv/bin/pip install -e '.[s3]'        # boto3 is an optional extra
cd deploy && docker compose down         # stack now lives on EC2
sudo systemctl restart beacon-collector
```

After this, hyperion runs **one systemd service and zero containers**.

### 4. What does not migrate

- **VictoriaMetrics history.** `vmbackup`/`vmrestore` is an unbuilt M5 item.
  Either build it first or accept a fresh start in the cloud.
- **Local Parquet.** Files already under `parquet_dir` are not retroactively
  uploaded. `aws s3 sync` them by hand if you want them.
- **Grafana annotations and any hand-edited dashboards.** Provisioned
  dashboards come from the bundle and are fine.

---

## Known gaps — read before Phase 2

**The NUC has no AWS credentials.** `infra/template.yaml` creates
`InstanceRole`, an instance profile for EC2 only. Nothing grants *hyperion*
write access to the bucket, and the Parquet tier uploads **directly to S3**,
bypassing EC2 entirely. With no credentials configured, every upload throws and
logs `s3 upload failed for <key> (kept locally)` — data stays safe on local
disk, but the "forever" tier silently never reaches S3. Add an IAM user (or
Roles Anywhere) with `PutObject` on that bucket and put the keys on hyperion.
This is the one piece of the push-to-AWS path that is not built.

**`AllowedCidr` assumes a static public IP at the lab.** The VictoriaMetrics
`/write` and Loki push APIs take no auth — no headers, no credentials — so the
security group's CIDR *is* the entire access control, over plain HTTP. If the
lab's ISP address is dynamic, ingestion breaks on every lease renewal. A VPN,
a tunnel, or a reverse proxy with basic auth are the alternatives.

**`rec.sh` v2 is untested on hardware.** The sticks have been unreachable since
2026-08-06. On first connect the collector notices rec.sh's md5 changed, kills
the old recorder and starts the new one, on every device. Three things need a
real device: that `getconf CLK_TCK` and `PAGESIZE` exist under toybox, that
`smaps_rollup` is readable as root on Android 11 *and* 14, and that the `#top`
line does not perturb the 1 Hz cadence.

---

## Rollback

Phase 2 → Phase 1: revert `endpoints:` to `localhost`, bring the local stack
back up with the `.local` override, restart the collector. The EC2 stack can
keep running; nothing breaks by leaving it.

Phase 1 → laptop: stop and disable `beacon-collector` on hyperion **before**
re-enabling the laptop's user unit. Never both.

---

## Reference

| port | service | who connects |
|---|---|---|
| 5555 | adb (on the stick) | collector → device |
| 8428 | VictoriaMetrics | collector writes, Grafana reads |
| 3100 | Loki | collector pushes, Grafana reads |
| 3000 | Grafana | you |

| command | what |
|---|---|
| `beacon probe <serial>` | which assumed commands work on a device |
| `beacon onboard <serial>` | apply + verify log-buffer remediation (§1.7) |
| `beacon run` | supervise and stream all owned devices |
| `beacon prune --dry-run` | report spool deletions without deleting |
| `scripts/e2e_local.py` | replay fixtures through VM + Loki and query back |
| `scripts/watch_for_stick.sh` | find a stick by MAC when DHCP moved it |
| `scripts/compact_parquet.py` | nightly hour→day Parquet compaction |

Identify sticks by **wlan0 MAC, not IP** — leases have moved across three
different addresses in three days. `D-005-02408` is `9c:b8:b4:77:79:b8`.

The devices do a nightly MDM reboot at ~22:00
(`sys.boot.reason = reboot,deviceowner`). The reachability gap is expected, not
a freeze.
