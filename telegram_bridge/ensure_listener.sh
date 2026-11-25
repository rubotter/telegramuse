#!/usr/bin/env bash
# Idempotent self-heal for the Telegram bridge listener.
#
# VM/container replacements can wipe /etc/systemd/system/*.service,
# which silently kills the bridge. The watchdog hook (see hook/) wakes
# an agent when the service is down, and the agent runs this script.
# It re-renders the unit from the template if missing, reloads systemd,
# enables and starts the service.
#
# Prints one line: OK (already running) or REPAIRED (had to fix).
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DST="/etc/systemd/system/telegram-bridge-listener.service"
SERVICE="telegram-bridge-listener"

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  # Active is not enough: a listener whose getUpdates fails forever is
  # still "active". The listener stamps state/heartbeat.json on every
  # successful poll; if it is stale (>180s), restart the service.
  HB="${TGBRIDGE_HOME:-$KIT_DIR}/state/heartbeat.json"
  stale="$(python3 - "$HB" <<'PYEOF'
import json, sys, time
try:
    ts = float(json.load(open(sys.argv[1], encoding="utf-8"))["ts"])
    print("0" if time.time() - ts <= 180 else "1")
except Exception:
    print("1")
PYEOF
)"
  if [[ "$stale" == "1" ]]; then
    sudo systemctl restart "$SERVICE" || { echo "FAILED restart (stale heartbeat)"; exit 1; }
    sleep 3
    if systemctl is-active --quiet "$SERVICE"; then
      echo "REPAIRED (restarted: stale polling heartbeat)"
      exit 0
    fi
    echo "FAILED still inactive after restart"
    exit 1
  fi
  echo "OK"
  exit 0
fi

if [[ ! -f "$UNIT_DST" ]]; then
  BRIDGE_USER="${SUDO_USER:-${USER:-$(id -un)}}"
  if [[ "$BRIDGE_USER" == "root" ]]; then BRIDGE_USER="$(logname 2>/dev/null || echo "$BRIDGE_USER")"; fi
  sed -e "s|<KIT_DIR>|$KIT_DIR|g" -e "s|<USER>|$BRIDGE_USER|g" \
    "$KIT_DIR/telegram-bridge-listener.service.example" \
    | sudo tee "$UNIT_DST" >/dev/null || { echo "FAILED render unit"; exit 1; }
fi
sudo systemctl daemon-reload || { echo "FAILED daemon-reload"; exit 1; }
sudo systemctl enable --now "$SERVICE" || { echo "FAILED enable"; exit 1; }
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  echo "REPAIRED"
else
  echo "FAILED still inactive"
  exit 1
fi
