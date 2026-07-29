#!/system/bin/sh
# beacon on-device 1 Hz recorder (plan.md §7, validated pattern §1.12).
# Launch DETACHED so it survives adb disconnects:
#   setsid sh /data/local/tmp/rec.sh >/dev/null 2>&1 </dev/null &
#
# Record format v1, one line per sample, pipe-separated:
#   uptime|mali_total_pages|app_pid|app_pages|MemAvailable_kB|CmaFree_kB|
#   temp0_milliC|temp1_milliC|load1 load5 load15|user,sys,idle;per-core...|rx,tx;
# Self-timestamped from /proc/uptime — cadence is ~1.1 s, never assume 1.0 (§1.12).
# Append-only; size rotation only (one mv), never rewrite-in-place (§7).

PKG="${1:-com.dolphin_us.dolphinstore}"
DIR="${2:-/data/local/tmp/beacon}"
OUT="$DIR/rec.log"
MAXSZ=16777216   # 16 MB, keep 2 files -> 1-2 days at ~184 B/s
GPU=/sys/kernel/debug/mali0/gpu_memory

mkdir -p "$DIR" 2>/dev/null

# single-instance guard: if a live recorder holds the pidfile, exit
PIDFILE="$DIR/rec.pid"
if [ -f "$PIDFILE" ]; then
  oldpid=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$oldpid" ] && [ -d "/proc/$oldpid" ] && grep -q rec.sh "/proc/$oldpid/cmdline" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$PIDFILE"

[ -f "$OUT" ] || echo "#beacon-rec v1 pkg=$PKG" >> "$OUT"

n=0
while true; do
  # uptime (primary self-timestamp)
  read up idle_ < /proc/uptime

  # app pid (first if several)
  pid=$(pidof "$PKG" 2>/dev/null)
  pid=${pid%% *}

  # mali: "mali0  total used_pages  NNN" header row; "kctx pid pages" rows
  total=""; app=""
  if [ -r "$GPU" ]; then
    while read f1 f2 f3 f4 f5; do
      if [ "$f1" = "mali0" ]; then
        total=$f4
      elif [ -n "$pid" ] && [ "$f2" = "$pid" ] && [ -n "$f3" ] && [ -z "$f4" ]; then
        app=$f3
      fi
    done < "$GPU"
  fi

  mem=""; cma=""
  while read k v u_; do
    case "$k" in
      MemAvailable:) mem=$v ;;
      CmaFree:) cma=$v ;;
    esac
  done < /proc/meminfo

  read t0 < /sys/class/thermal/thermal_zone0/temp 2>/dev/null
  read t1 < /sys/class/thermal/thermal_zone1/temp 2>/dev/null

  read l1 l5 l15 rest_ < /proc/loadavg

  cpus=""
  while read c u ni s i rest_; do
    case "$c" in
      cpu[0-9]*) cpus="$cpus$u,$s,$i;" ;;
    esac
  done < /proc/stat

  net=""
  while read line; do
    case "$line" in
      *wlan0:*) rest=${line#*:}; set -- $rest; net="${net}wlan0,$1,$9;" ;;
      *eth0:*)  rest=${line#*:}; set -- $rest; net="${net}eth0,$1,$9;" ;;
    esac
  done < /proc/net/dev

  echo "$up|$total|${pid:-0}|${app:-0}|$mem|$cma|$t0|$t1|$l1 $l5 $l15|$cpus|$net" >> "$OUT"

  # rotation check every 60 samples: one mv, fresh append (§7)
  n=$((n+1))
  if [ $n -ge 60 ]; then
    n=0
    sz=$(stat -c %s "$OUT" 2>/dev/null)
    if [ -n "$sz" ] && [ "$sz" -gt $MAXSZ ]; then
      mv "$OUT" "$OUT.1"
      echo "#beacon-rec v1 pkg=$PKG rotated" >> "$OUT"
    fi
  fi

  sleep 1
done
