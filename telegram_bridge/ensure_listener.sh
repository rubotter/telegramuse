#!/usr/bin/env bash
# Idempotent self-heal for the Telegram bridge listener.
#
# VM/container replacements can wipe /etc/systemd/system/*.service,
# which silently kills the bridge. The watchdog hook (see hook/) wakes
# an agent when the service is down, and the agent runs this script.
# It re-renders the unit whenever the rendered content differs from
# what is installed (kit moved, template changed, user changed),
# reloads systemd, enables and starts the service, and only reports
# REPAIRED once the listener's polling heartbeat is actually fresh
# again — "active" alone proved nothing in the past.
#
# Prints one line: OK (already running) or REPAIRED (had to fix) or
# FAILED ... (needs a human). Requires sudo for systemctl and for
# writing the unit file. Without systemd it cannot heal anything and
# says so (start telegram_bridge/listener.py manually instead).
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DST="/etc/systemd/system/telegram-bridge-listener.service"
SERVICE="telegram-bridge-listener"

if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  echo "FAILED no systemd on this machine; start telegram_bridge/listener.py manually"
  exit 1
fi

# bridge.env holds the bot token: it must not sit at the default umask.
# Fix the permissions instead of installing happily over a 644 secret.
ENV_FILE="$KIT_DIR/bridge.env"
if [[ -f "$ENV_FILE" ]]; then
  perms="$(stat -c %a "$ENV_FILE" 2>/dev/null || stat -f %Lp "$ENV_FILE" 2>/dev/null || echo "")"
  if [[ -n "$perms" && "$perms" != "600" && "$perms" != "400" ]]; then
    chmod 600 "$ENV_FILE" 2>/dev/null \
      && echo "note: tightened $ENV_FILE permissions to 600 (was $perms)" >&2 \
      || { echo "FAILED bridge.env permissions are $perms; chmod 600 it manually"; exit 1; }
  fi
fi

# Resolve the bridge home exactly like telegram_bridge/common.py:
# environment, then TGBRIDGE_HOME in the kit root's bridge.env, then
# the kit root. The heartbeat must be read from where the listener
# actually writes it.
BRIDGE_HOME="${TGBRIDGE_HOME:-}"
if [[ -z "$BRIDGE_HOME" && -f "$ENV_FILE" ]]; then
  BRIDGE_HOME="$(grep -E '^[[:space:]]*TGBRIDGE_HOME[[:space:]]*=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
fi
BRIDGE_HOME="${BRIDGE_HOME:-$KIT_DIR}"
HB="$BRIDGE_HOME/state/heartbeat.json"

heartbeat_ts() {
  python3 - "$HB" <<'PYEOF'
import json, sys
try:
    print(float(json.load(open(sys.argv[1], encoding="utf-8"))["ts"]))
except Exception:
    print(0)
PYEOF
}

# Wait (up to ~40s) for a heartbeat NEWER than $1 (epoch seconds).
# A restarted listener needs getMe + one long poll before it stamps a
# heartbeat, so the old 3-second is-active check reported REPAIRED for
# services that were still broken (e.g. revoked token).
wait_for_fresh_heartbeat() {
  local since="$1" i ts
  for i in $(seq 1 20); do
    sleep 2
    if ! systemctl is-active --quiet "$SERVICE"; then
      return 1
    fi
    ts="$(heartbeat_ts)"
    if python3 -c "import sys; sys.exit(0 if float('$ts') > float('$since') else 1)"; then
      return 0
    fi
  done
  return 1
}

render_unit() {
  # Render via Python string replacement, not sed: kit paths may
  # contain spaces or sed metacharacters (|, &) that silently corrupt
  # a sed-based render.
  BRIDGE_USER="${SUDO_USER:-${USER:-$(id -un)}}"
  if [[ "$BRIDGE_USER" == "root" ]]; then BRIDGE_USER="$(logname 2>/dev/null || echo "$BRIDGE_USER")"; fi
  python3 - "$KIT_DIR/telegram-bridge-listener.service.example" "$KIT_DIR" "$BRIDGE_USER" <<'PYEOF'
import sys
tpl, kit, user = sys.argv[1], sys.argv[2], sys.argv[3]
with open(tpl, encoding="utf-8") as f:
    text = f.read()
sys.stdout.write(text.replace("<KIT_DIR>", kit).replace("<USER>", user))
PYEOF
}

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  now="$(date +%s)"
  ts="$(heartbeat_ts)"
  if python3 -c "import sys; sys.exit(0 if $now - float('$ts') <= 180 else 1)"; then
    echo "OK"
    exit 0
  fi
  # Active but not polling: restart, then require a NEW heartbeat.
  before="$ts"
  sudo systemctl restart "$SERVICE" || { echo "FAILED restart (stale heartbeat)"; exit 1; }
  if wait_for_fresh_heartbeat "$before"; then
    echo "REPAIRED (restarted: stale polling heartbeat)"
    exit 0
  fi
  echo "FAILED restarted but polling heartbeat did not recover"
  exit 1
fi

# Service down: (re)render the unit if the installed copy differs from
# the current template render (missing, kit moved, template updated).
rendered="$(render_unit)" || { echo "FAILED render unit"; exit 1; }
installed=""
[[ -f "$UNIT_DST" ]] && installed="$(cat "$UNIT_DST")"
unit_changed=0
if [[ "$rendered" != "$installed" ]]; then
  printf '%s' "$rendered" | sudo tee "$UNIT_DST" >/dev/null || { echo "FAILED write unit"; exit 1; }
  unit_changed=1
fi
sudo systemctl daemon-reload || { echo "FAILED daemon-reload"; exit 1; }
start_since="$(date +%s)"
sudo systemctl enable --now "$SERVICE" || { echo "FAILED enable"; exit 1; }
if (( unit_changed )); then
  # enable --now does not restart an already-running service; a changed
  # unit must be applied explicitly.
  sudo systemctl restart "$SERVICE" || { echo "FAILED restart after unit change"; exit 1; }
fi
if wait_for_fresh_heartbeat "$start_since"; then
  echo "REPAIRED"
else
  echo "FAILED service started but polling heartbeat did not become fresh"
  exit 1
fi
