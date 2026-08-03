#!/usr/bin/env bash
# Watch the LAN for D-005-02408 returning on ANY address, then capture.
#
# Why not just poll one IP: this stick's DHCP lease moves. It was on .106 on
# 07-27, .100 on 07-28, and was ACKed .104 at 00:01:04 on 07-29 — 16 minutes
# before the freeze. Polling a fixed address would miss it.
#
# Identifies by wlan0 MAC, and also reports any unknown new host in case
# Android hands out a randomized MAC after the reboot.
set -uo pipefail

STICK_MAC="9c:b8:b4:77:79:b8"   # from dmesg: [dhd] Register interface [wlan0]
KNOWN="192.168.0.1|192.168.0.103|192.168.0.104|192.168.0.105"  # router | this box | D-005-01860 | TV box
ADB="${BEACON_ADB:-$HOME/Android/Sdk/platform-tools/adb}"
DEADLINE=$(( $(date +%s) + 900 ))                    # 15 minutes

echo "watching for $STICK_MAC on 192.168.0.0/24 (also flagging unknown hosts)"

found_ip=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  # populate ARP: fan out pings across the /24
  for i in $(seq 1 254); do (ping -c1 -W1 "192.168.0.$i" >/dev/null 2>&1 &); done
  sleep 5

  # 1. exact MAC match
  ip=$(ip neigh show dev wlo1 2>/dev/null | grep -i "$STICK_MAC" | awk '{print $1}' | head -1)
  if [ -n "$ip" ]; then found_ip="$ip"; echo "MAC MATCH: $STICK_MAC at $ip"; break; fi

  # 2. fallback: any host we don't recognise (covers MAC randomization)
  unknown=$(ip neigh show dev wlo1 2>/dev/null | grep -iE "REACHABLE|STALE" \
            | awk '{print $1}' | grep -vE "^($KNOWN)$" | head -1)
  if [ -n "$unknown" ]; then
    # only claim it if adb answers on 5555
    if timeout 5 bash -c "echo > /dev/tcp/$unknown/5555" 2>/dev/null; then
      found_ip="$unknown"; echo "UNKNOWN HOST with :5555 open at $unknown"; break
    fi
    echo "  (unknown host $unknown, no :5555 — ignoring)"
  fi
  echo "  $(date +%H:%M:%S) not yet"
done

if [ -z "$found_ip" ]; then
  echo "=== TIMEOUT: stick never reappeared on the LAN ==="
  exit 1
fi

echo "=== $found_ip is up at $(date +%H:%M:%S) — waiting 8s for adbd ==="
sleep 8
cd "$(dirname "$0")/.."
"$ADB" connect "$found_ip:5555" 2>&1
./scripts/freeze_forensics.sh "$found_ip:5555"

if [ "$found_ip" != "192.168.0.100" ]; then
  echo
  echo "!! NOTE: stick is at $found_ip, but config/beacon.yaml pins 192.168.0.100."
  echo "!! The collector will NOT reconnect until that host: line is updated."
fi
