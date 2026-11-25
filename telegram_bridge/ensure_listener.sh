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
  echo "OK"
  exit 0
fi

if [[ ! -f "$UNIT_DST" ]]; then
  sed -e "s|<KIT_DIR>|$KIT_DIR|g" -e "s|<USER>|${USER:-$(id -un)}|g" \
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
