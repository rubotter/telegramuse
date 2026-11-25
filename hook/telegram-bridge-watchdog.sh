#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Watchdog for the Telegram bridge listener: VM/container replacements
# can wipe the systemd unit, which silently kills the bridge. Poll at
# the hook's interval (5s recommended); silent while healthy, wake when
# down so a worker can run the self-heal script
# (telegram_bridge/ensure_listener.sh).
#
# Healthy means the listener's polling heartbeat is fresh — and, when
# systemd is present, the service is also active. A process can be
# "active" while getUpdates fails forever (bad token, dead network);
# is-active alone misses that. On a machine WITHOUT systemd (listener
# started by hand), is-active can never succeed, so the watchdog falls
# back to the heartbeat alone instead of crying wolf every poll; the
# worker then knows the listener must be checked/restarted manually,
# because ensure_listener.sh only drives systemd.
#
# Paths resolve exactly like telegram_bridge/common.py: TGBRIDGE_HOME
# from the environment, else from the kit root's bridge.env, else the
# kit root — so a home set only in bridge.env cannot make the watchdog
# watch the wrong directory and restart a healthy listener forever.
#
# Wake cooldown: if the fault cannot be healed by a restart (e.g. a
# revoked token), an unconditional wake every poll would loop forever.
# So wakes share ONE global cooldown (not per-reason: a fault flapping
# between "inactive" and "heartbeat_stale" must not earn a fresh wake
# each time it changes shape). The cooldown is recorded only AFTER a
# wake actually succeeds — a failed wake does not consume it — and it
# clears only after TWO consecutive healthy polls, so a single lucky
# poll during a flap does not reset the damping.

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${TGBRIDGE_HOME:-}" && -f "$KIT_DIR/bridge.env" ]]; then
  _home_from_env="$(grep -E '^[[:space:]]*TGBRIDGE_HOME[[:space:]]*=' "$KIT_DIR/bridge.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
  if [[ -n "$_home_from_env" ]]; then
    TGBRIDGE_HOME="$_home_from_env"
  fi
fi
BASE="${TGBRIDGE_HOME:-$KIT_DIR}"
HEARTBEAT="$BASE/state/heartbeat.json"
WAKE_STATE="$BASE/state/watchdog-wake.json"
WAKE_COOLDOWN=600
mkdir -p "$BASE/state"

HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  HAVE_SYSTEMD=1
fi

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

read_wake_state() {
  python3 - "$WAKE_STATE" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(int(d.get("ts", 0)), int(d.get("healthy_streak", 0)))
except Exception:
    print(0, 0)
PYEOF
}

write_wake_state() {
  python3 - "$WAKE_STATE" "$1" "$2" <<'PYEOF'
import json, os, sys
p, ts, streak = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
tmp = f"{p}.tmp.{os.getpid()}"
with open(tmp, "w") as f:
    json.dump({"ts": ts, "healthy_streak": streak}, f)
os.replace(tmp, p)
PYEOF
}

# wake_unless_cooling <reason> <payload>
wake_unless_cooling() {
  local reason="$1" payload="$2"
  local now last streak
  now="$(date +%s)"
  read -r last streak < <(read_wake_state) || { last=0; streak=0; }
  if (( now - last < WAKE_COOLDOWN )); then
    log "watchdog_wake_cooling" "{\"reason\":\"$reason\"}"
    silent "watchdog already woke recently; cooling down" '{}'
    return 0
  fi
  if wake "telegram-bridge-listener unhealthy: $reason" "$payload"; then
    # Cooldown starts only once the wake is actually delivered.
    write_wake_state "$now" 0
  else
    # A failed wake must not consume the cooldown: the fault is still
    # unreported, so the next poll should try to wake again.
    log "watchdog_wake_failed" "{\"reason\":\"$reason\"}"
    echo "watchdog wake failed for $reason; will retry next poll" >&2
    exit 1
  fi
}

note_healthy() {
  # Clear the cooldown only after two consecutive healthy polls.
  if [[ -f "$WAKE_STATE" ]]; then
    local last streak
    read -r last streak < <(read_wake_state) || { last=0; streak=0; }
    streak=$((streak + 1))
    if (( streak >= 2 )); then
      rm -f "$WAKE_STATE"
    else
      write_wake_state "$last" "$streak"
    fi
  fi
  log "listener_ok" '{}'
  silent "telegram-bridge-listener is healthy" '{}'
}

if [[ "$HAVE_SYSTEMD" == "1" ]] && ! systemctl is-active --quiet telegram-bridge-listener 2>/dev/null; then
  log "listener_down" '{}'
  wake_unless_cooling "inactive" '{"service":"telegram-bridge-listener","reason":"inactive"}'
elif ! heartbeat_fresh; then
  log "listener_heartbeat_stale" '{}'
  wake_unless_cooling "heartbeat_stale" '{"service":"telegram-bridge-listener","reason":"heartbeat_stale"}'
else
  note_healthy
fi
