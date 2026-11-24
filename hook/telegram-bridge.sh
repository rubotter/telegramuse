#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Telegram bridge detector: every poll, look for NEW lines in the local
# inbox queue written by the bridge listener
# (~/hooks/state/telegram-bridge-inbox.jsonl). No new message -> silent.
# New messages -> wake with up to 20 of them in the payload.
# Checkpoint = highest update_id already handed to the worker, kept in
# ~/hooks/state/telegram-bridge-checkpoint.json. Dry runs never advance it.

INBOX="$HOME/hooks/state/telegram-bridge-inbox.jsonl"
CHECKPOINT="$HOME/hooks/state/telegram-bridge-checkpoint.json"
OUT_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE"' EXIT

python3 - "$INBOX" "$CHECKPOINT" "$OUT_FILE" <<'PYEOF'
import json, os, sys

inbox_path, checkpoint_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
dry = os.environ.get("HATCH_HOOK_DRY_RUN") == "1"

last = 0
if os.path.exists(checkpoint_path):
    try:
        last = int(json.load(open(checkpoint_path, encoding="utf-8")).get("last_update_id", 0))
    except Exception:
        last = 0

msgs = []
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
            if int(m.get("update_id", 0)) > last:
                msgs.append(m)

msgs.sort(key=lambda m: int(m.get("update_id", 0)))
batch = msgs[:20]

if batch and not dry:
    new_last = int(batch[-1].get("update_id", last))
    tmp = checkpoint_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_update_id": new_last}, f)
    os.replace(tmp, checkpoint_path)

result = {"decision": "NEW" if batch else "NONE", "messages": batch, "queued_total": len(msgs)}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)
print(result["decision"])
PYEOF

DECISION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('decision','NONE'))" "$OUT_FILE")"
PAYLOAD="$(cat "$OUT_FILE")"

case "$DECISION" in
  NEW)
    log "new_telegram_messages" "$PAYLOAD"
    wake "New Telegram messages arrived in the bridge inbox queue" "$PAYLOAD"
    ;;
  *)
    log "no_new_telegram_messages" "$PAYLOAD"
    silent "No new Telegram messages in the bridge inbox queue" "$PAYLOAD"
    ;;
esac
