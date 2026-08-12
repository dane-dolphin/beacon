#!/usr/bin/env bash
# Post-deploy / any-time health check. Read-only: it starts nothing, stops
# nothing, and changes no state.
#
#   scripts/check.sh            fast checks only (~2 s)
#   scripts/check.sh --probe    also probe every live device (slow, needs adb)
#
# Exit status is 0 if nothing is obviously broken, 1 otherwise, so it can be
# used from cron or a wrapper.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEACON="$ROOT/.venv/bin/beacon"
LOG="$ROOT/var/beacon.log"
: "${BEACON_ADB:=/opt/android/platform-tools/adb}"
export BEACON_ADB
PROBE=0
[ "${1:-}" = "--probe" ] && PROBE=1

problems=0
say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
bad()  { printf '  \033[31m!!\033[0m %s\n' "$1"; problems=$((problems+1)); }
ok()   { printf '  \033[32mok\033[0m %s\n' "$1"; }
note() { printf '     %s\n' "$1"; }

say "services"
# `systemctl is-active` says "inactive" for a unit that does not exist, which
# would report a box that never installed the console as broken. Distinguish
# absent from stopped, and look at user units too (the dev laptop runs one).
unit_state() {
  if systemctl cat "$1" >/dev/null 2>&1; then systemctl is-active "$1" 2>/dev/null
  elif systemctl --user cat "$1" >/dev/null 2>&1; then
    echo "user:$(systemctl --user is-active "$1" 2>/dev/null)"
  else echo absent; fi
}
for unit in beacon-collector beacon-console; do
  case "$(unit_state "$unit")" in
    active|user:active) ok "$unit active" ;;
    absent)             note "$unit not installed" ;;
    *)                  bad "$unit is stopped or failed — systemctl status $unit" ;;
  esac
done
for c in beacon-vm beacon-loki beacon-grafana; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ]; then
    ok "$c running"
  else
    bad "$c not running — cd deploy && docker compose -f docker-compose.yml -f docker-compose.local.yml up -d"
  fi
done

say "endpoints"
for probe_url in "http://localhost:8428/health|VictoriaMetrics" \
                 "http://localhost:3100/ready|Loki" \
                 "http://localhost:3000/api/health|Grafana"; do
  url=${probe_url%%|*}; name=${probe_url##*|}
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null || echo 000)
  [ "$code" = "200" ] && ok "$name $code" || bad "$name returned $code ($url)"
done

say "devices"
if [ -x "$BEACON" ]; then
  "$BEACON" devices 2>&1 | sed 's/^/  /'
else
  bad "no venv at $BEACON — python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
fi

say "discovery + connection activity (last 15)"
if [ -r "$LOG" ]; then
  grep -E "adopted|\\bmoved\\b|streaming|unreachable|last known address" "$LOG" \
    | tail -15 | sed 's/^/  /'
  [ -z "$(grep -E 'adopted|streaming' "$LOG" | tail -1)" ] &&
    bad "no device has ever streamed — check adb reachability"
else
  note "no log at $LOG yet"
fi

say "shipping failures"
if [ -r "$LOG" ]; then
  # only since the newest collector start: old failures are not news
  since=$(grep -n "collector .* starting" "$LOG" | tail -1 | cut -d: -f1)
  since=${since:-1}
  n=$(tail -n "+$since" "$LOG" | grep -c "push failed" || true)
  [ "$n" = "0" ] && ok "no Loki/VM push failures since last start" \
                 || bad "$n push failure(s) since last start — tail -n +$since $LOG | grep 'push failed' | tail -3"
  m=$(tail -n "+$since" "$LOG" | grep -c "rejected entries by timestamp" || true)
  [ "$m" != "0" ] && note "$m timestamp rejections (expected while a spool backlog drains; lines are in Parquet)"
fi

say "rec.sh v2 — per-process tier"
# NOTE: the spool is JSONL, so the recorder line lives INSIDE a json field.
# Anchoring with ^#top can never match, whatever the device produced.
tops=$(grep -rho '#top[^"]*' "$ROOT"/var/spool/*/rec/*/*.jsonl 2>/dev/null | tail -1)
if [ -n "$tops" ]; then
  ok "#top lines present — per-process RAM/CPU is working"
  note "${tops:0:120}..."
else
  bad "no #top lines yet: Process Resources will be empty"
  note "if devices are streaming, check on-device: adb shell getconf CLK_TCK"
  note "and whether /proc/<pid>/smaps_rollup is readable as root"
fi

if [ "$PROBE" = "1" ] && [ -x "$BEACON" ]; then
  say "probe (live devices)"
  serials=$("$BEACON" devices --json 2>/dev/null \
    | python3 -c 'import json,sys,time
d=json.load(sys.stdin)
now=d["now"]
for x in d["devices"]:
    if not x["skip"] and x["last_seen"] and now-x["last_seen"] < 900:
        print(x["serial"])' 2>/dev/null)
  [ -z "$serials" ] && note "no recently-live devices to probe"
  for s in $serials; do
    printf '  --- %s\n' "$s"
    "$BEACON" probe "$s" 2>/dev/null | python3 -c '
import json, sys
try: r = json.load(sys.stdin)
except Exception: print("      probe produced no JSON (device unreachable?)"); raise SystemExit
bad = [k for k, v in r.items() if not v["ok"]]
print(f"      {len(r)-len(bad)}/{len(r)} passed" + (f"  FAILED: {\", \".join(bad)}" if bad else ""))'
  done
fi

say "summary"
if [ "$problems" = "0" ]; then
  printf '  \033[32mall checks passed\033[0m\n'
else
  printf '  \033[31m%d problem(s) above\033[0m\n' "$problems"
fi
exit $(( problems > 0 ? 1 : 0 ))
