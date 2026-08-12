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

**Why `/opt/beacon`.** `/opt` is the FHS location for add-on software and is
the conventional home for something that runs as a system service. `$HOME`
works too and needs no `sudo`/`chown`, but `/opt` keeps the checkout
independent of any one user account — worth it if the collector ever moves to a
dedicated service user. One caveat if you *do* put it under `/home` on some
other machine: a `/home` on a separately-mounted or per-user-encrypted
filesystem is not available when a system service starts at boot.

```bash
sudo git clone <repo-url> /opt/beacon
```

If the remote is not set up, copy from the dev machine instead. The repo minus
build artifacts is ~2 MB and `.git` comes along, so history stays intact and
you can push from either box later. rsync copies the **working tree**, so
uncommitted changes come too and nothing needs pushing first:

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
sudo chown -R "$USER" /opt/beacon    # so the venv and var/ are yours to write
cd /opt/beacon
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'     # add '[s3]' only in Phase 2
.venv/bin/pytest -q                   # expect 41 passed
```

### 3. Observability stack

Put the secrets in `deploy/.env` rather than exporting them. Compose reads that
file regardless of who invokes it, which matters because **`sudo` strips your
environment** (`env_reset` is the sudoers default): `export
GRAFANA_ADMIN_PASSWORD=… ; sudo docker compose up -d` silently yields a Grafana
on the default `beacon-dev`, with no error. In Phase 2 the same thing empties
`LOKI_S3_BUCKET`. `.env` sidesteps it, and mirrors how EC2 already works via
`/etc/beacon.env`.

```bash
cd /opt/beacon/deploy
cat > .env <<'EOF'
GRAFANA_ADMIN_PASSWORD=<something-not-beacon-dev>
EOF
chmod 600 .env

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

Running compose under `sudo` is fine — the daemon is root either way, and
membership in the `docker` group is effectively root-equivalent, so this is a
convenience choice, not a security one. If you do use `sudo`, use it for
*every* compose call: `sudo docker compose up -d` followed by an unprivileged
`docker compose ps` reports permission denied and looks like nothing is
running. The collector is unaffected either way — it never talks to Docker;
`After=docker.service` in its unit is ordering only.

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

**Leave `paths:` alone.** They are relative to the repo root, and the unit
below sets `WorkingDirectory` to exactly that, so `./var/...` resolves
correctly — the same arrangement the dev laptop has been running on. Only make
them absolute if you move the data off the repo directory.

```bash
mkdir -p /opt/beacon/var        # systemd creates the log FILE, not its directory
```

`endpoints:` stay on `localhost` in Phase 1.

**Discovery — adopt every adb device on the LAN.** Off by default. Turn it on
for the office bench so a new device is collected without editing this file:

```yaml
discovery:
  enabled: true
  subnet: 192.168.0.0/24     # the PHYSICAL LAN, never a routed/tailnet range
  port: 5555
  interval: 300              # seconds between sweeps
  skip_serials: []           # anything on the wifi to leave alone
```

Each sweep TCP-probes every address on the subnet (a /24 takes seconds, 64
probes in flight), `adb connect`s whatever answers, and reads `ro.serialno`.
A serial it has not seen is adopted and gets its own supervisor; a serial it
knows at a **new address** is relocated in place — no restart, no lost stream
cursors. That second half is the fix for a DHCP lease moving, which is what
made `D-005-02408` retry a dead IP for 13 hours on 2026-07-29.

Devices listed in `devices:` still work exactly as before. A device the YAML
assigns to a *different* `nuc:` is never adopted — §2.3 ownership still holds
where it was stated explicitly.

Three things to be deliberate about:

- Adopted devices get `adb root`, `logd --reinit` + `logcat -G`, and the
  detached recorder. That **modifies the device**. Fine for a bench you own.
- Keep `subnet:` on the physical LAN. Tailscale is installed on hyperion, and
  pointing a sweep at a routed range is the one way "the other NUC is on a
  different network" stops being true.
- With discovery on, `enabled: false` matters on any *other* NUC that copies
  this config, or both will adopt everything and double-ingest.

An unreachable device is no longer silent. Each failed cycle counts, and the
log carries the address, consecutive-failure count and elapsed downtime
(throttled after the third), plus a `stick_device_down_seconds{serial}` series
for the dashboard.

### 5. systemd unit

**The unit is not in the repo.** It exists only as a *user* unit on the dev
laptop and has to be written for any new box. Write
`/etc/systemd/system/beacon-collector.service`:

```ini
[Unit]
Description=Beacon collector — streams Dolphin Sticks to VM/Loki/Parquet
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=hyperion
WorkingDirectory=/opt/beacon
Environment=BEACON_ADB=/opt/android/platform-tools/adb
ExecStart=/opt/beacon/.venv/bin/beacon run
Restart=always
RestartSec=10
StandardOutput=append:/opt/beacon/var/beacon.log
StandardError=append:/opt/beacon/var/beacon.log

[Install]
WantedBy=multi-user.target
```

**Paths here must be literal.** systemd does not expand `$HOME` or `~` in
`WorkingDirectory`, `ExecStart` or `StandardOutput` — a unit written with
`$HOME` fails to start with a confusing "not an absolute path" error. Adjust
`User=` and the three paths if your username is not `hyperion`.

The collector never talks to Docker, so it needs no group membership and no
socket access; `After=docker.service` is ordering only, so the stack is up
before the collector starts pushing to it.

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
tail -f /opt/beacon/var/beacon.log
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
15 3 * * *  /opt/beacon/.venv/bin/python \
            /opt/beacon/scripts/compact_parquet.py \
            /opt/beacon/var/parquet
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

### Reaching Grafana from another machine

The compose file publishes `3000:3000`, which binds every interface — nothing
to open, no firewall rule. Docker publishes ports through its own iptables
chain, so ufw is usually not what is blocking if it fails.

| from | URL |
|---|---|
| hyperion itself | <http://localhost:3000> |
| another box on the same LAN | `http://<hyperion-lan-ip>:3000` |
| anywhere, over Tailscale | `http://<hyperion-tailscale-ip>:3000` |

```bash
# on hyperion
ip -4 addr show | grep inet          # LAN address
tailscale ip -4                      # 100.x.y.z
tailscale status                     # MagicDNS names, and who is online
```

**Use the raw `100.x` address, not the MagicDNS name.** hyperion's hostname is
`pop-os`, which is the Pop!_OS default — if the dev laptop is on the same
tailnet it is *also* `pop-os`, and Tailscale will have silently renamed one of
them to `pop-os-1`. The IP is unambiguous.

Log in as `admin` with the password from `deploy/.env` — or whatever you last
set with `grafana cli admin reset-admin-password`, which wins over `.env` on an
already-initialised Grafana.

The same applies to the other two ports if you want them directly:
VictoriaMetrics on `:8428`, Loki on `:3100`. Neither has authentication, which
is fine over a tailnet and is exactly why `AllowedCidr` matters in Phase 2.

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

## Shipping a change to hyperion

Develop on the dev machine, land it in git, pull on hyperion. What you re-run
afterwards depends entirely on *what* changed — it is never "restart
everything".

```bash
# dev machine
.venv/bin/pytest -q                       # do not ship red
git add -A && git commit -m "..." && git push

# hyperion
cd /opt/beacon && git pull
```

Note the two machines may authenticate to GitHub differently — hyperion's
remote is HTTPS, the dev laptop's is SSH. If `git fetch` fails on one, check
`git remote -v` before assuming the repo is broken.

| what changed | what to re-run |
|---|---|
| `collector/**/*.py` | `sudo systemctl restart beacon-collector`. No reinstall — `pip install -e` imports from the source tree |
| `config/beacon.yaml` | `sudo systemctl restart beacon-collector` |
| `device/rec.sh` | **nothing.** The collector compares md5 on the next connect, kills the old recorder and relaunches — on *every* device, so expect a brief 1 Hz gap fleet-wide |
| `deploy/grafana/dashboards/*.json` | **nothing.** Provisioner polls every 30 s |
| `deploy/grafana/provisioning/*` | `docker compose … up -d` — datasources load at Grafana startup, not on a poll |
| `deploy/docker-compose*.yml`, `deploy/loki/*.yaml`, `deploy/.env` | `docker compose … up -d`; only changed services are recreated |
| `pyproject.toml` (new dependency) | `.venv/bin/pip install -e '.[dev]'`, then restart the collector |

Where `docker compose …` means the full Phase 1 invocation:

```bash
cd /opt/beacon/deploy
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

Two things that are never needed on a code change: recreating the venv, and
`docker compose down`. Bringing the stack down loses nothing by itself, but
`down -v` deletes `vmdata`, `lokidata` and `grafanadata` — that is a real data
loss once telemetry is flowing.

After a collector restart, confirm it actually came back:

```bash
systemctl status beacon-collector --no-pager
tail -n 40 /opt/beacon/var/beacon.log
```

Look for `streaming (boot_epoch=…)` per device. Repeating
`streaming` / `ended cleanly (EOF)` / `will reconnect` every few seconds is the
collect-nothing reconnect loop, not healthy operation.

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
# then, on the INSTANCE (SSH or SSM Session Manager) — note /opt/beacon:
# that is where the template's user-data untars the bundle, unrelated to
# where the repo lives on hyperion.
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
