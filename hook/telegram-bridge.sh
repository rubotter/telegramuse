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
# woke up.) TGBRIDGE_HOME resolution matches telegram_bridge/common.py:
# environment first, then TGBRIDGE_HOME inside the kit root's
# bridge.env, then the kit root. Setting it only in bridge.env (as
# bridge.env.example suggests) therefore works for every component.
#
# Checkpoint = highest update_id already handed to the worker PLUS the
# byte offset up to which the inbox has been consumed, kept next to the
# inbox in state/telegram-bridge-checkpoint.json. Polls read only the
# bytes appended since the offset (a rotated/replaced inbox — smaller
# than the offset — restarts from 0). The checkpoint is advanced ONLY
# after wake succeeds: if the wake fails, the same messages are offered
# again on the next poll instead of being lost. Dry runs never
# advance it.
#
# Hook logs record metadata only (ids, chat ids, character counts) —
# never message text. Chat content belongs in the queue, not in a
# long-lived log file.

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${TGBRIDGE_HOME:-}" && -f "$KIT_DIR/bridge.env" ]]; then
  _home_from_env="$(grep -E '^[[:space:]]*TGBRIDGE_HOME[[:space:]]*=' "$KIT_DIR/bridge.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
  if [[ -n "$_home_from_env" ]]; then
    TGBRIDGE_HOME="$_home_from_env"
  fi
fi
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
offset = 0
if os.path.exists(checkpoint_path):
    try:
        cp = json.load(open(checkpoint_path, encoding="utf-8"))
        last = int(cp.get("last_update_id", 0))
        offset = int(cp.get("inbox_offset", 0))
    except Exception:
        last, offset = 0, 0

msgs = []
seen = set()
new_offset = offset
if os.path.exists(inbox_path):
    size = os.path.getsize(inbox_path)
    if size < offset:
        offset = 0  # inbox was rotated/replaced; rescan from the start
    with open(inbox_path, "rb") as f:
        f.seek(offset)
        data = f.read()
    # Consume only complete lines; a trailing partial line belongs to
    # a write still in flight and is picked up next poll.
    end = data.rfind(b"\n")
    consumed = data[: end + 1] if end >= 0 else b""
    new_offset = offset + len(consumed)
    line_ends = []  # (update_id, byte offset just past that line)
    pos = offset
    for raw in consumed.splitlines(keepends=True):
        pos += len(raw)
        line = raw.decode("utf-8", errors="replace").strip()
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
            line_ends.append((uid, pos))

msgs.sort(key=lambda m: int(m.get("update_id", 0)))
batch = msgs[:20]
if batch:
    # Advance the offset only past the last DISPATCHED message's line:
    # lines beyond the 20-message batch must be re-read next poll or
    # those messages would be skipped forever. (Lines re-read with an
    # update_id at or below the checkpoint are filtered out above.)
    dispatched_last = int(batch[-1].get("update_id", 0))
    for uid, line_end in line_ends:
        if uid == dispatched_last:
            new_offset = line_end
            break

meta = {
    "count": len(batch),
    "queued_total": len(msgs),
    "update_ids": [int(m.get("update_id", 0)) for m in batch],
    "chat_ids": sorted({m.get("chat_id") for m in batch if m.get("chat_id") is not None}),
    "text_chars": sum(len(m.get("text") or "") for m in batch),
}
result = {
    "decision": "NEW" if batch else "NONE",
    "messages": batch,
    "queued_total": len(msgs),
    "new_last": int(batch[-1].get("update_id", last)) if batch else last,
    "new_offset": new_offset,
    "meta": meta,
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)
print(result["decision"])
PYEOF

DECISION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('decision','NONE'))" "$OUT_FILE")"
META="$(python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get('meta',{}), ensure_ascii=False))" "$OUT_FILE")"
PAYLOAD="$(cat "$OUT_FILE")"

case "$DECISION" in
  NEW)
    log "new_telegram_messages" "$META"
    if [[ "${HATCH_HOOK_DRY_RUN:-}" == "1" ]]; then
      # Dry run: report what WOULD happen, never wake, never commit
      # the checkpoint (a dry run must not consume real messages).
      silent "dry run: would wake with new Telegram messages" "$PAYLOAD"
    elif wake "New Telegram messages arrived in the bridge inbox queue" "$PAYLOAD"; then
      # Wake delivered: only now commit the checkpoint (ids + offset).
      python3 - "$CHECKPOINT" "$OUT_FILE" <<'PYEOF'
import json, os, sys
checkpoint_path, out_path = sys.argv[1], sys.argv[2]
result = json.load(open(out_path, encoding="utf-8"))
tmp = f"{checkpoint_path}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"last_update_id": int(result.get("new_last", 0)),
               "inbox_offset": int(result.get("new_offset", 0))}, f)
os.replace(tmp, checkpoint_path)
PYEOF
    else
      log "wake_failed_checkpoint_kept" "$META"
      echo "wake failed; checkpoint NOT advanced, messages will be re-offered" >&2
      exit 1
    fi
    ;;
  *)
    log "no_new_telegram_messages" "$META"
    silent "No new Telegram messages in the bridge inbox queue" "$PAYLOAD"
    ;;
esac
