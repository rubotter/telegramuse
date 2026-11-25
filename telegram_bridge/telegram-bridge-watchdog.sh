#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Watchdog for the Telegram bridge listener: VM/container replacements
# can wipe the systemd unit, which silently kills the bridge. Poll at
# the hook's interval (5s recommended); silent while the service is
# active, wake only when it is down so a worker can run the self-heal
# script (telegram_bridge/ensure_listener.sh).

if systemctl is-active --quiet telegram-bridge-listener 2>/dev/null; then
  log "listener_ok" '{}'
  silent "telegram-bridge-listener is active" '{}'
else
  log "listener_down" '{}'
  wake "telegram-bridge-listener is not active" '{"service":"telegram-bridge-listener"}'
fi
