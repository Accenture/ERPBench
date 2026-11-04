#!/bin/bash
# Wait until Xvfb is ready on DISPLAY=:1 before starting dependent processes.
# Polls xdpyinfo every 0.2s, gives up after 10s.

set -e
DISPLAY="${DISPLAY:-:1}"
deadline=$(( $(date +%s) + 10 ))

while ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "wait-for-display: timed out waiting for $DISPLAY" >&2
    exit 1
  fi
  sleep 0.2
done
