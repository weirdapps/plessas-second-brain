#!/bin/bash
# Bounded readiness gate: block until the VPS answers ssh, or give up.
#
# The scheduled pull jobs win the scheduling race and lose the network race.
# com.plessas.trading-data-sync has RunAtLoad=true and fired 47s into a boot,
# before any default route existed ("No route to host"); the second-brain
# db-pull fired at 13:52:19 on 2026-08-29, ONE SECOND after a DarkWake, into a
# radio that had not associated yet. Both did a SINGLE-SHOT reachability check,
# failed it, and did nothing until their next slot hours later. The runs either
# side succeeded, so nothing was wrong except the timing.
#
# Polling is free when the network is already up: the first probe succeeds and
# this returns in well under a second, so a healthy run is unchanged.
#
# usage: wait-for-vps.sh [host] [budget_seconds] [interval_seconds]
#   exit 0  as soon as ssh succeeds
#   exit 1  when the budget is exhausted (caller keeps its own failure handling)

HOST="${1:-vps}"
BUDGET="${2:-180}"
INTERVAL="${3:-10}"

deadline=$(( $(date +%s) + BUDGET ))

while :; do
  # Held rather than discarded so the real diagnostic ("No route to host",
  # "Permission denied") survives to the caller's log instead of being replaced
  # by a generic timeout line.
  err=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" true 2>&1) && exit 0
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep "$INTERVAL"
done

echo "wait-for-vps: $HOST still unreachable after ${BUDGET}s${err:+, last error: $err}" >&2
exit 1
