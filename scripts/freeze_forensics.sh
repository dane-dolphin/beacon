#!/usr/bin/env bash
# Post-freeze forensic capture for a Dolphin Stick.
#
# Context: D-005-02408 hard-froze 2026-07-29 00:17:55 CDT on a Mali GPU reset
# that never completed (see NOTES). Run this the FIRST thing after the device
# comes back — several of these artifacts are destroyed by normal operation.
#
#   ./scripts/freeze_forensics.sh                 # default serial/addr
#   ./scripts/freeze_forensics.sh 192.168.0.100:5555
#   ./scripts/freeze_forensics.sh usb             # USB-attached device
#
set -uo pipefail

ADB="${BEACON_ADB:-$HOME/Android/Sdk/platform-tools/adb}"
TARGET="${1:-192.168.0.100:5555}"
OUT="var/forensics/$(date +%Y%m%dT%H%M%S)"

if [ "$TARGET" = "usb" ]; then
  SEL=()                                  # single USB device
else
  "$ADB" connect "$TARGET" >/dev/null 2>&1
  SEL=(-s "$TARGET")
fi

mkdir -p "$OUT"
echo "capturing to $OUT (target: $TARGET)"

sh_() { "$ADB" "${SEL[@]}" shell "$@" 2>&1; }
grab() { # grab <outfile> <shell command...>
  local f="$1"; shift
  echo "  -> $f"
  sh_ "$@" > "$OUT/$f"
}

# --- 1. Volatile first: the pre-reset kernel ring buffer -------------------
# ramoops lives in RAM and is gone after a cold power cut. This is the only
# record of what the kernel printed AFTER our dmesg stream died at 00:17:55.
echo "[1/6] pre-reset kernel log (most perishable)"
sh_ "ls -la /sys/fs/pstore/ 2>/dev/null"      > "$OUT/pstore-listing.txt"
sh_ "cat /sys/fs/pstore/console-ramoops* 2>/dev/null" > "$OUT/pstore-console.txt"
sh_ "cat /sys/fs/pstore/dmesg-ramoops*   2>/dev/null" > "$OUT/pstore-dmesg.txt"
sh_ "cat /proc/last_kmsg 2>/dev/null"         > "$OUT/last_kmsg.txt"
for f in pstore-console pstore-dmesg last_kmsg; do
  [ -s "$OUT/$f.txt" ] && echo "  ** $f.txt HAS CONTENT — this is the good one **"
done

# --- 2. Why did it reset, and did a watchdog fire? ------------------------
echo "[2/6] boot reason + watchdog"
grab boot-reason.txt   "getprop | grep -iE 'boot.reason|bootmode|ro.boot'"
grab uptime.txt        "cat /proc/uptime; date"
grab watchdog.txt      "dmesg | grep -iE 'watchdog|hung task|rcu_sched|soft lockup|panic|reboot'"

# --- 3. The 1 Hz on-device trace ------------------------------------------
# NOTE: recorder._current_boot_lines() replays ONLY the newest boot segment,
# so the collector will NOT ingest the pre-freeze data. Pull it by hand.
echo "[3/6] on-device recorder log (collector will NOT backfill this)"
"$ADB" "${SEL[@]}" pull /data/local/tmp/beacon/rec.log   "$OUT/rec.log"   2>&1 | tail -1
"$ADB" "${SEL[@]}" pull /data/local/tmp/beacon/rec.log.1 "$OUT/rec.log.1" 2>&1 | tail -1

# --- 4. Thread stacks: who was blocked on the GPU -------------------------
echo "[4/6] ANR traces + tombstones"
sh_ "ls -la /data/anr/ /data/tombstones/ 2>/dev/null" > "$OUT/crash-listing.txt"
"$ADB" "${SEL[@]}" pull /data/anr         "$OUT/anr"         2>&1 | tail -1
"$ADB" "${SEL[@]}" pull /data/tombstones  "$OUT/tombstones"  2>&1 | tail -1

# --- 5. Mali state --------------------------------------------------------
echo "[5/6] mali / GPU state"
grab mali-gpu_memory.txt "cat /sys/kernel/debug/mali0/gpu_memory"
grab mali-dmesg.txt      "dmesg | grep -i mali"
grab gpu-blocked.txt     "ps -A -o pid,stat,name | grep -E '^\s*[0-9]+\s+D'"

# --- 6. Crash buffer + current logs ---------------------------------------
echo "[6/6] logcat crash buffer + full dmesg"
grab logcat-crash.txt  "logcat -b crash -d"
grab logcat-events.txt "logcat -b events -d -t 2000"
grab dmesg-full.txt    "dmesg"

echo
echo "done: $OUT"
echo "start with pstore-console.txt / last_kmsg.txt — everything after"
echo "uptime 8067.00 is the window we have no data for."
