#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Telegram bridge detector: every poll, look for NEW lines in the inbox
# queue written by the bridge listener. No new message -> silent.
# New messages -> wake with up to 20 of them in the payload.
#
# Paths: the inbox lives under TGBRIDGE_HOME (the listener's base dir),
# which defaults to the kit root — the SAME directory the listener
# writes to. (An earlier version read ~/hooks/state/... while the
# listener wrote <kit>/inbox.jsonl, so a default deployment never
# woke up.) If the listener runs with a custom TGBRIDGE_HOME, export
# the same value for this hook.
#
# Checkpoint = highest update_id already handed to the worker, kept
# next to the inbox in state/telegram-bridge-checkpoint.json. It is
# advanced ONLY after wake succeeds: if the wake fails, the same
# messages are offered again on the next poll instead of being lost.
# Dry runs never advance it.

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${TGBRIDGE_HOME:-$KIT_DIR}"
INBOX="$BASE/inbox.jsonl"
CHECKPOINT="$BASE/state/telegram-bridge-checkpoint.json"
mkdir -p "$BASE/state"
OUT_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE"' EXIT

python3 - "$INBOX" "$CHECKPOINT" "$OUT_FILE" <<'PYEOF'
import json, os, sys

inbox_path, checkpoint_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

last = 0
if os.path.exists(checkpoint_path):
    try:
        last = int(json.load(open(checkpoint_path, encoding="utf-8")).get("last_update_id", 0))
    except Exception:
        last = 0

msgs = []
seen = set()
if os.path.exists(inbox_path):
    with open(inbox_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            uid = int(m.get("update_id", 0))
            # Dedupe: a listener crash between inbox append and offset
            # save can redeliver the same update_id; never dispatch it
            # twice, neither within a batch nor across polls.
            if uid > last and uid not in seen:
                seen.add(uid)
                msgs.append(m)

msgs.sort(key=lambda m: int(m.get("update_id", 0)))
batch = msgs[:20]

result = {
    "decision": "NEW" if batch else "NONE",
    "messages": batch,
    "queued_total": len(msgs),
    "new_last": int(batch[-1].get("update_id", last)) if batch else last,
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)
print(result["decision"])
PYEOF

DECISION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('decision','NONE'))" "$OUT_FILE")"
PAYLOAD="$(cat "$OUT_FILE")"

case "$DECISION" in
  NEW)
    log "new_telegram_messages" "$PAYLOAD"
    if [[ "${HATCH_HOOK_DRY_RUN:-}" == "1" ]]; then
      # Dry run: report what WOULD happen, never wake, never commit
      # the checkpoint (a dry run must not consume real messages).
      silent "dry run: would wake with new Telegram messages" "$PAYLOAD"
    elif wake "New Telegram messages arrived in the bridge inbox queue" "$PAYLOAD"; then
      # Wake delivered: only now commit the checkpoint.
      python3 - "$CHECKPOINT" "$OUT_FILE" <<'PYEOF'
import json, os, sys
checkpoint_path, out_path = sys.argv[1], sys.argv[2]
new_last = int(json.load(open(out_path, encoding="utf-8")).get("new_last", 0))
tmp = checkpoint_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"last_update_id": new_last}, f)
os.replace(tmp, checkpoint_path)
PYEOF
    else
      log "wake_failed_checkpoint_kept" "$PAYLOAD"
      echo "wake failed; checkpoint NOT advanced, messages will be re-offered" >&2
      exit 1
    fi
    ;;
  *)
    log "no_new_telegram_messages" "$PAYLOAD"
    silent "No new Telegram messages in the bridge inbox queue" "$PAYLOAD"
    ;;
esac
