#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Watchdog for the Telegram bridge listener: VM/container replacements
# can wipe the systemd unit, which silently kills the bridge. Poll at
# the hook's interval (5s recommended); silent while healthy, wake when
# down so a worker can run the self-heal script
# (telegram_bridge/ensure_listener.sh).
#
# Healthy means BOTH: the service is active AND the listener's polling
# heartbeat is fresh. A process can be "active" while getUpdates fails
# forever (bad token, dead network); is-active alone misses that.
#
# Wake cooldown: if the fault cannot be healed by a restart (e.g. a
# revoked token), an unconditional wake every poll would loop forever
# — wake the worker, restart, still broken, wake again. So a wake for
# the same reason repeats at most once per WAKE_COOLDOWN seconds; the
# state file clears as soon as the bridge is healthy again.

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${TGBRIDGE_HOME:-$KIT_DIR}"
HEARTBEAT="$BASE/state/heartbeat.json"
WAKE_STATE="$BASE/state/watchdog-wake.json"
WAKE_COOLDOWN=600

heartbeat_fresh() {
  python3 - "$HEARTBEAT" <<'PYEOF'
import json, sys, time
try:
    ts = float(json.load(open(sys.argv[1], encoding="utf-8"))["ts"])
except Exception:
    sys.exit(1)
sys.exit(0 if time.time() - ts <= 180 else 1)
PYEOF
}

# wake_unless_cooling <reason> <payload>
wake_unless_cooling() {
  local reason="$1" payload="$2"
  local now last=0 last_reason=""
  now="$(date +%s)"
  if [[ -f "$WAKE_STATE" ]]; then
    read -r last last_reason < <(python3 -c "
import json, sys
try:
    d = json.load(open('$WAKE_STATE'))
    print(d.get('ts', 0), d.get('reason', ''))
except Exception:
    print(0, '')
") || true
  fi
  if [[ "$last_reason" == "$reason" ]] && (( now - last < WAKE_COOLDOWN )); then
    log "watchdog_wake_cooling" "{\"reason\":\"$reason\"}"
    silent "watchdog already woke for $reason recently; cooling down" '{}'
    return 0
  fi
  mkdir -p "$(dirname "$WAKE_STATE")"
  python3 -c "
import json, os
p = '$WAKE_STATE'
tmp = p + '.tmp'
with open(tmp, 'w') as f:
    json.dump({'ts': $now, 'reason': '$reason'}, f)
os.replace(tmp, p)
"
  wake "telegram-bridge-listener unhealthy: $reason" "$payload"
}

if ! systemctl is-active --quiet telegram-bridge-listener 2>/dev/null; then
  log "listener_down" '{}'
  wake_unless_cooling "inactive" '{"service":"telegram-bridge-listener","reason":"inactive"}'
elif ! heartbeat_fresh; then
  log "listener_heartbeat_stale" '{}'
  wake_unless_cooling "heartbeat_stale" '{"service":"telegram-bridge-listener","reason":"heartbeat_stale"}'
else
  rm -f "$WAKE_STATE"
  log "listener_ok" '{}'
  silent "telegram-bridge-listener is active and polling" '{}'
fi
