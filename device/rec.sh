#!/system/bin/sh
# beacon on-device recorder (plan.md §7, validated pattern §1.12).
# Launch DETACHED so it survives adb disconnects:
#   setsid sh /data/local/tmp/rec.sh >/dev/null 2>&1 </dev/null &
#
# TWO record formats, told apart by the first character of the line.
#
# 1 Hz sample line — UNCHANGED from v1, 11 pipe-separated fields:
#   uptime|mali_total_pages|app_pid|app_pages|MemAvailable_kB|CmaFree_kB|
#   temp0_milliC|temp1_milliC|load1 load5 load15|user,sys,idle;per-core...|rx,tx;
#
# #top line — v2 addition, every TOPINT samples:
#   #top|uptime|MemTotal_kB|clk_tck|name,pid,rss_kb,pss_kb,utime,stime;...
#
# The 1 Hz line is deliberately NOT extended. rec.sh is the process that wedged
# during the 2026-07-29 freeze (it blocked reading mali debugfs and died 0.9 s
# before the kernel's first fault), so the hot loop stays exactly as validated
# and every new syscall lives on the low-frequency line instead. A side benefit:
# a v1 parser keeps working on v2 output untouched.
#
# Self-timestamped from /proc/uptime — cadence is ~1.1 s, never assume 1.0 (§1.12).
# Append-only; size rotation only (one mv), never rewrite-in-place (§7).

PKG="${1:-com.dolphin_us.dolphinstore}"
DIR="${2:-/data/local/tmp/beacon}"
TOPINT="${3:-30}"   # samples between #top lines (~1.1 s each, so ~33 s)
OUT="$DIR/rec.log"
MAXSZ=16777216   # 16 MB, keep 2 files -> 1-2 days at ~184 B/s
GPU=/sys/kernel/debug/mali0/gpu_memory
TOPN=5           # hard cap: every distinct name is a permanent VM series (§5.1)

# Constants read once — MemTotal never changes, and paying for it every second
# was the point of the "capture MemTotal once" note.
PGKB=$(( $(getconf PAGESIZE 2>/dev/null || echo 4096) / 1024 ))
CLK=$(getconf CLK_TCK 2>/dev/null || echo 100)
MEMTOTAL=""
while read k v u_; do
  case "$k" in MemTotal:) MEMTOTAL=$v; break ;; esac
done < /proc/meminfo

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

# NOTE: the header is written only when the file is ABSENT (§7, append-only), so
# a device upgraded in place keeps its v1 header while emitting v2 #top lines.
# Nothing parses this header for versioning — line shape decides — but do not
# start trusting it as a version marker.
[ -f "$OUT" ] || echo "#beacon-rec v2 pkg=$PKG" >> "$OUT"

# Top-N processes by RSS, then PSS/CPU for just those N.
#
# Deliberately NOT in the 1 Hz path: this walks every /proc/<pid> and forks a
# handful of times. Two passes on purpose — ranking uses statm (one builtin
# read per pid, no fork), and only the surviving N pay for cmdline/stat/smaps.
#
# Residual risk, accepted: a process wedged in D state can still block a /proc
# read here, and shell has no read timeout. That would stall this line and the
# 1 Hz tier with it — the same class of hang as the freeze. It is bounded to
# once per TOPINT samples instead of once per second, which is the whole point.
emit_top() {
  up_now="$1"
  blk=$(
    for p in /proc/[0-9]*; do
      read sz_ res_ rest_ < "$p/statm" 2>/dev/null || continue
      echo "$res_ ${p#/proc/}"
    done | sort -nr | head -n $TOPN | while read res pid; do
      [ -d "/proc/$pid" ] || continue          # exited between the passes
      # argv[0] holds the FULL Android process name; comm truncates to 15 chars
      name=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -n 1)
      [ -n "$name" ] || read name < "/proc/$pid/comm" 2>/dev/null
      [ -n "$name" ] || continue
      name=${name##*/}                          # /system/bin/foo -> foo
      name=$(echo "$name" | tr ',;|' '___')     # separators must not appear
      st=$(cat "/proc/$pid/stat" 2>/dev/null) || continue
      rest=${st#*') '}                          # comm can hold spaces and parens
      set -- $rest                              # $1 is now field 3 (state)
      [ $# -ge 13 ] || continue
      shift 11                                  # $1 = field 14 (utime)
      ut=$1; stm=$2
      # PSS is the honest number for "who is using memory" — summing RSS
      # double-counts the zygote's shared framework pages. smaps_rollup is
      # absent on older kernels and unreadable without root; 0 means
      # unavailable, not zero-sized, and the collector drops the series.
      # A brace group, not a subshell: pss must survive into this scope. The
      # stderr redirect is on the GROUP because the open failure is emitted by
      # the redirect itself, where a plain `2>/dev/null` on `done <` misses it.
      # ([ -r ] is not enough either — /proc/<pid>/smaps_rollup is mode 0444
      # but the open is refused by ptrace_may_access when not root.)
      pss=0
      { while read k v u_; do
          case "$k" in Pss:) pss=$v; break ;; esac
        done < "/proc/$pid/smaps_rollup"; } 2>/dev/null
      printf '%s,%s,%s,%s,%s,%s;' "$name" "$pid" "$((res * PGKB))" "$pss" "$ut" "$stm"
    done
  )
  [ -n "$blk" ] && echo "#top|$up_now|$MEMTOTAL|$CLK|$blk" >> "$OUT"
}

n=0
tn=$TOPINT       # emit one immediately rather than waiting a full interval
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

  # low-frequency tier: top-N processes, well off the 1 Hz path
  tn=$((tn+1))
  if [ $tn -ge $TOPINT ]; then
    tn=0
    emit_top "$up"
  fi

  # rotation check every 60 samples: one mv, fresh append (§7)
  n=$((n+1))
  if [ $n -ge 60 ]; then
    n=0
    sz=$(stat -c %s "$OUT" 2>/dev/null)
    if [ -n "$sz" ] && [ "$sz" -gt $MAXSZ ]; then
      mv "$OUT" "$OUT.1"
      echo "#beacon-rec v2 pkg=$PKG rotated" >> "$OUT"
    fi
  fi

  sleep 1
done
