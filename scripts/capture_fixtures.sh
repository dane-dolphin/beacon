#!/usr/bin/env bash
# Capture REAL fixtures from a Dolphin Stick into collector/tests/fixtures/
# (read-only: nothing on the device is modified). Run whenever the reference
# device is reachable; the committed fixtures are provisional reconstructions
# from plan.md's measured samples until then.
#
# Usage: scripts/capture_fixtures.sh [host:port]   (default 192.168.0.106:5555)
set -euo pipefail

ADDR="${1:-192.168.0.106:5555}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/collector/tests/fixtures"
ADB="${BEACON_ADB:-$(command -v adb || echo "$HOME/Android/Sdk/platform-tools/adb")}"

echo "using adb: $ADB ; device: $ADDR"
"$ADB" connect "$ADDR"
"$ADB" -s "$ADDR" root >/dev/null 2>&1 || echo "warning: adb root failed"
sleep 2
"$ADB" connect "$ADDR" >/dev/null

echo "== gpu_memory (root) =="
"$ADB" -s "$ADDR" shell cat /sys/kernel/debug/mali0/gpu_memory | tee "$OUT/gpu_memory.txt" | head -6

echo "== dmesg tail (root) =="
"$ADB" -s "$ADDR" shell dmesg | tail -300 > "$OUT/dmesg_sample.txt"
grep -cE "mali|GPU" "$OUT/dmesg_sample.txt" || true

echo "== logcat: 60s threadtime capture (all buffers) =="
timeout 60 "$ADB" -s "$ADDR" logcat -v threadtime -b all > "$OUT/logcat_sample.txt" || true
wc -l "$OUT/logcat_sample.txt"

echo "== app file log excerpt =="
"$ADB" -s "$ADDR" shell "tail -n 200 /sdcard/Android/data/com.dolphin_us.dolphinstore/files/Logs/Dolphin_File.log" \
  > "$OUT/dolphin_file_log_sample.txt" || echo "app file log not readable"

echo "== recorder sample (launches rec.sh for 15s if not running) =="
"$ADB" -s "$ADDR" push "$ROOT/device/rec.sh" /data/local/tmp/rec.sh >/dev/null
"$ADB" -s "$ADDR" shell "setsid sh /data/local/tmp/rec.sh >/dev/null 2>&1 </dev/null &"
sleep 15
"$ADB" -s "$ADDR" shell "tail -n 15 /data/local/tmp/beacon/rec.log" > "$OUT/rec_sample.txt"
cat "$OUT/rec_sample.txt"

echo
echo "fixtures updated in $OUT — run: .venv/bin/pytest -q"
