#!/usr/bin/env bash
# Install the Telegram bridge listener as a systemd service.
# Usage:  ./install.sh
# Expects bridge.env to exist in the kit dir (see bridge.env.example).
# Requires: Python >= 3.10, systemd, and sudo (unit file + service).
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=/etc/systemd/system/telegram-bridge-listener.service

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "Python >= 3.10 is required (found: $(python3 --version 2>&1 || echo none))." >&2
  echo "The bridge code uses 3.10+ syntax (e.g. dict | None annotations evaluated at def time)." >&2
  exit 1
fi

if [[ ! -f "$KIT_DIR/bridge.env" ]]; then
  echo "bridge.env missing. Create it from bridge.env.example first:" >&2
  echo "  cp bridge.env.example bridge.env && chmod 600 bridge.env  # then fill in the token" >&2
  exit 1
fi
# bridge.env holds the bot token; never leave it at the default umask.
perms="$(stat -c %a "$KIT_DIR/bridge.env" 2>/dev/null || stat -f %Lp "$KIT_DIR/bridge.env" 2>/dev/null || echo "")"
if [[ -n "$perms" && "$perms" != "600" && "$perms" != "400" ]]; then
  chmod 600 "$KIT_DIR/bridge.env"
  echo "note: tightened bridge.env permissions to 600 (was $perms)."
fi
if [[ ! -f "$KIT_DIR/allowlist.json" ]]; then
  cp "$KIT_DIR/allowlist.example.json" "$KIT_DIR/allowlist.json"
  echo "allowlist.json created in pairing mode (owner_id null)."
fi

# Resolve the real desktop user: under sudo, $USER can be root,
# which would render User=root and recreate the permission mismatch.
BRIDGE_USER="${SUDO_USER:-$USER}"
if [[ "$BRIDGE_USER" == "root" ]]; then BRIDGE_USER="$(logname 2>/dev/null || echo "$BRIDGE_USER")"; fi
# Render via Python string replacement, not sed: kit paths may contain
# spaces or sed metacharacters (|, &) that silently corrupt sed output.
rendered="$(python3 - "$KIT_DIR/telegram-bridge-listener.service.example" "$KIT_DIR" "$BRIDGE_USER" <<'PYEOF'
import sys
tpl, kit, user = sys.argv[1], sys.argv[2], sys.argv[3]
with open(tpl, encoding="utf-8") as f:
    text = f.read()
sys.stdout.write(text.replace("<KIT_DIR>", kit).replace("<USER>", user))
PYEOF
)"
installed=""
[[ -f "$UNIT" ]] && installed="$(cat "$UNIT")"
unit_changed=0
if [[ "$rendered" != "$installed" ]]; then
  printf '%s' "$rendered" | sudo tee "$UNIT" >/dev/null
  unit_changed=1
  echo "systemd unit written/updated."
fi

sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bridge-listener
if (( unit_changed )) && systemctl is-active --quiet telegram-bridge-listener; then
  # enable --now leaves an already-running service on the OLD unit;
  # restart so a re-install actually applies template/path changes.
  sudo systemctl restart telegram-bridge-listener
  echo "service restarted to apply the updated unit."
fi
sleep 2
systemctl is-active telegram-bridge-listener
echo "Installed. Logs: journalctl -u telegram-bridge-listener -f"
