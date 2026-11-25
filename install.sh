#!/usr/bin/env bash
# Install the Telegram bridge listener as a systemd service.
# Usage:  ./install.sh
# Expects bridge.env to exist in the kit dir (see bridge.env.example).
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=/etc/systemd/system/telegram-bridge-listener.service

if [[ ! -f "$KIT_DIR/bridge.env" ]]; then
  echo "bridge.env missing. Create it from bridge.env.example first:" >&2
  echo "  cp bridge.env.example bridge.env && chmod 600 bridge.env  # then fill in the token" >&2
  exit 1
fi
if [[ ! -f "$KIT_DIR/allowlist.json" ]]; then
  cp "$KIT_DIR/allowlist.example.json" "$KIT_DIR/allowlist.json"
  echo "allowlist.json created in pairing mode (owner_id null)."
fi

# Resolve the real desktop user: under sudo, $USER can be root,
# which would render User=root and recreate the permission mismatch.
BRIDGE_USER="${SUDO_USER:-$USER}"
if [[ "$BRIDGE_USER" == "root" ]]; then BRIDGE_USER="$(logname 2>/dev/null || echo "$BRIDGE_USER")"; fi
sed -e "s|<KIT_DIR>|$KIT_DIR|g" -e "s|<USER>|$BRIDGE_USER|g" \
  "$KIT_DIR/telegram-bridge-listener.service.example" | sudo tee "$UNIT" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bridge-listener
sleep 2
systemctl is-active telegram-bridge-listener
echo "Installed. Logs: journalctl -u telegram-bridge-listener -f"
