#!/usr/bin/env python3
"""Telegram bridge listener.

Long-polls getUpdates and appends every incoming message to the local
inbox queue (one JSON object per line) that an agent-side poller (see
hook/) watches. Also drives the animated "thinking" placeholder: the
moment the owner's message is queued, a placeholder message is sent and
animated until send_reply.py deletes it when the real reply goes out.

Run in the foreground; systemd supervises it (unit template included).
Token comes from the TELEGRAM_BOT_TOKEN env var (see common.py).
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telegram_bridge.common import (  # noqa: E402
    ALLOWLIST_PATH, INBOX_PATH, KNOWN_CHATS_PATH, STATE_DIR, THINKING_PATH,
    api_call, load_json, save_json, token_fingerprint,
)

OFFSET_PATH = STATE_DIR / "offset.json"
BOT_PATH = STATE_DIR / "bot.json"
HEARTBEAT_PATH = STATE_DIR / "heartbeat.json"
STREAK_PATH = STATE_DIR / "bot-streak.json"

# Non-text message kinds, in detection order. The queue only carries
# text, so without a type marker a photo/sticker/voice message is
# indistinguishable from an empty text message downstream.
_MSG_TYPE_KEYS = (
    "photo", "sticker", "voice", "video", "video_note", "audio",
    "document", "animation", "location", "contact", "poll",
)


def msg_type(msg):
    if msg.get("text"):
        return "text"
    for key in _MSG_TYPE_KEYS:
        if msg.get(key) is not None:
            return key
    return "other"


def note_known_chats(chat_ids):
    """Remember every chat the listener has received a message from.

    send_reply.py refuses to send to chats outside this set (unless
    --force): the bridge should only ever talk where it has been
    talked to, which blunts prompt-injection attempts to make the
    worker message arbitrary chats.
    """
    known = load_json(KNOWN_CHATS_PATH, [])
    if not isinstance(known, list):
        known = []
    current = set(known)
    new = {c for c in chat_ids if c is not None} - current
    if new:
        save_json(KNOWN_CHATS_PATH, sorted(current | new))


def last_queued_update_id():
    """Highest update_id already in the inbox (0 if none).

    If the process dies after appending to the inbox but before saving
    the getUpdates offset, Telegram redelivers the same updates after
    restart; comparing against the inbox tail keeps them out twice.
    """
    try:
        with open(INBOX_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return 0
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.strip().splitlines()):
            try:
                return int(json.loads(line).get("update_id", 0))
            except Exception:
                continue
    except Exception:
        pass
    return 0


def reset_bot_streak(chat_id):
    """A human spoke in this chat: reset the bot-to-bot streak counter
    (see send_reply.py) so bot exchanges may resume."""
    state = load_json(STREAK_PATH, {})
    if state.get(str(chat_id)) not in (None, 0):
        state[str(chat_id)] = 0
        save_json(STREAK_PATH, state)

# --- Thinking placeholder style (customise freely) -----------------------
# Native "typing" chat action + a moon-phase cycle in the message text.
THINKING_FRAMES = [
    "🌑 思考中…", "🌒 思考中…", "🌓 思考中…", "🌔 思考中…",
    "🌕 思考中…", "🌖 思考中…", "🌗 思考中…", "🌘 思考中…",
]
THINKING_INTERVAL = 1.0   # seconds per frame (deadline-paced, see loop)
GROUP_THINKING_INTERVAL = 4.0  # groups: ~20 bot actions/min limit, stay well under
TYPING_REFRESH = 4.0      # sendChatAction typing lasts ~5s server-side
THINKING_TTL = 600.0      # placeholder auto-deleted after 10 minutes
_thinking_lock = threading.Lock()


def owner_id():
    return load_json(ALLOWLIST_PATH, {}).get("owner_id")


def start_thinking(chat_id, chat_type=None):
    """Send the animated thinking placeholder for one chat, once."""
    key = str(chat_id)
    with _thinking_lock:
        if key in load_json(THINKING_PATH, {}):
            return
    try:
        resp = api_call("sendMessage", {"chat_id": chat_id, "text": THINKING_FRAMES[0]})
    except Exception as e:
        print(f"thinking send failed: {e}", flush=True)
        return
    if not resp.get("ok"):
        print(f"thinking send not ok: {resp}", flush=True)
        return
    try:
        api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass
    with _thinking_lock:
        state = load_json(THINKING_PATH, {})
        state[key] = {
            "message_id": resp["result"]["message_id"],
            "started": time.time(),
            "frame": 0,
            "last_action": time.time(),
            "last_edit": time.time(),
            "chat_type": chat_type or "private",
        }
        save_json(THINKING_PATH, state)


def animation_loop():
    while True:
        cycle_start = time.time()
        with _thinking_lock:
            state = load_json(THINKING_PATH, {})
        if not state:
            time.sleep(THINKING_INTERVAL)
            continue
        changed = False
        removed = set()
        for key in list(state.keys()):
            entry = state.get(key)
            if not entry:
                continue
            chat_id = int(key)
            mid = entry["message_id"]
            if time.time() - entry.get("started", 0) > THINKING_TTL:
                try:
                    api_call("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                except Exception:
                    pass
                del state[key]
                removed.add(key)
                changed = True
                continue
            # Group chats have a much lower bot rate limit (~20 actions
            # /min per group): a 1s edit cadence there triggers HTTP 429.
            # Groups therefore animate at GROUP_THINKING_INTERVAL and
            # refresh typing less often; private chats keep 1s cadence.
            is_group = entry.get("chat_type") in ("group", "supergroup")
            typing_refresh = 8.0 if is_group else TYPING_REFRESH
            edit_interval = GROUP_THINKING_INTERVAL if is_group else THINKING_INTERVAL
            if time.time() - entry.get("last_action", 0) >= typing_refresh:
                try:
                    api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                except Exception:
                    pass
                entry["last_action"] = time.time()
                changed = True
            if time.time() - entry.get("last_edit", 0) < edit_interval:
                continue
            frame = (entry.get("frame", 0) + 1) % len(THINKING_FRAMES)
            try:
                resp = api_call(
                    "editMessageText",
                    {"chat_id": chat_id, "message_id": mid, "text": THINKING_FRAMES[frame]},
                )
            except Exception:
                resp = None
            if not resp or not resp.get("ok"):
                # Tolerate transient failures (e.g. a flood-control hit
                # at a fast cadence); drop only after repeated failures,
                # which means the message is really gone (deleted by
                # send_reply.py) or no longer editable.
                entry["fails"] = entry.get("fails", 0) + 1
                if entry["fails"] >= 3:
                    del state[key]
                    removed.add(key)
                changed = True
                continue
            entry["fails"] = 0
            entry["frame"] = frame
            entry["last_edit"] = time.time()
            changed = True
        if changed:
            with _thinking_lock:
                # Merge against the freshest file instead of blindly
                # overwriting: send_reply.py may have cleared an entry
                # (real reply sent) while this cycle was editing it, and
                # the main thread may have added a new one. Entries the
                # loop removed on purpose stay removed.
                current = load_json(THINKING_PATH, {})
                merged = dict(current)
                for k in removed:
                    # Only drop the entry if it is still the very record
                    # this loop removed. A new placeholder created for
                    # the same chat in the meantime (different
                    # message_id) must survive, or it becomes an orphan
                    # nobody ever deletes.
                    cur = current.get(k)
                    if cur is not None and cur.get("message_id") != state.get(k, {}).get("message_id"):
                        continue
                    merged.pop(k, None)
                for k, v in state.items():
                    cur = current.get(k)
                    if cur is None:
                        continue
                    # A newer placeholder for the same chat (different
                    # message_id, created while this cycle was editing
                    # the old one) owns the chat now; the stale snapshot
                    # must not overwrite it.
                    if cur.get("message_id") != v.get("message_id"):
                        continue
                    merged[k] = v
                save_json(THINKING_PATH, merged)
        # Pace by deadline, not fixed sleep: an edit round-trip can take
        # most of the frame budget, so sleep only what is left.
        time.sleep(max(0.05, THINKING_INTERVAL - (time.time() - cycle_start)))


CHECKPOINT_PATH = STATE_DIR / "telegram-bridge-checkpoint.json"
INBOX_ROTATE_LINES = 20000


def maybe_rotate_inbox(inbox_lines):
    """Drop inbox lines the agent hook has already checkpointed.

    The listener is the only writer and this runs on its own thread
    between appends, so the rewrite cannot race another writer; the
    hook reads via atomic replace. Keeps the hook's per-poll full scan
    from growing without bound. Returns the new approximate line count.
    """
    if inbox_lines <= INBOX_ROTATE_LINES:
        return inbox_lines
    last = 0
    try:
        last = int(load_json(CHECKPOINT_PATH, {}).get("last_update_id", 0))
    except Exception:
        last = 0
    if last <= 0:
        return inbox_lines
    kept = []
    try:
        with open(INBOX_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    if int(json.loads(line).get("update_id", 0)) > last:
                        kept.append(line)
                except Exception:
                    continue
    except Exception:
        return inbox_lines
    tmp = f"{INBOX_PATH}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(kept)
    os.replace(tmp, INBOX_PATH)
    return len(kept)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)

    # The cached getMe is only valid for the token it was fetched with;
    # a changed token (or a different bot) invalidates it, otherwise
    # the own-message filter would use a stale bot_id.
    try:
        fp = token_fingerprint()
    except Exception:
        fp = None
    bot = load_json(BOT_PATH, None)
    if not bot or (fp is not None and bot.get("_token_fp") != fp):
        try:
            me = api_call("getMe")
        except Exception as e:
            print(f"getMe failed: {e} (check TELEGRAM_BOT_TOKEN)", flush=True)
            sys.exit(1)
        if not me.get("ok"):
            print(f"getMe failed: {me}", flush=True)
            sys.exit(1)
        bot = me["result"]
        bot["_token_fp"] = fp
        save_json(BOT_PATH, bot)
    bot_id = bot.get("id")
    print(f"listener up: bot @{bot.get('username')} id={bot_id}", flush=True)

    threading.Thread(target=animation_loop, daemon=True).start()

    offset = load_json(OFFSET_PATH, {"offset": 0}).get("offset", 0)
    last_queued = last_queued_update_id()
    try:
        with open(INBOX_PATH, "rb") as f:
            inbox_lines = sum(1 for _ in f)
    except Exception:
        inbox_lines = 0

    while True:
        inbox_lines = maybe_rotate_inbox(inbox_lines)
        try:
            resp = api_call(
                "getUpdates",
                {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                timeout=35,
            )
        except Exception as e:
            print(f"getUpdates error: {e}; retry in 10s", flush=True)
            time.sleep(10)
            continue
        if not resp.get("ok"):
            print(f"getUpdates not ok: {resp}; retry in 10s", flush=True)
            time.sleep(10)
            continue

        # Heartbeat: proof that polling itself is alive, not just the
        # process. The watchdog treats a stale heartbeat (>180s) as a
        # failure even when systemd still reports the service active.
        save_json(HEARTBEAT_PATH, {"ts": time.time()})

        updates = resp.get("result", [])
        if not updates:
            continue

        lines = []
        queued = []
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            if u["update_id"] <= last_queued:
                continue  # redelivery after a crash; already in the inbox
            msg = u.get("message")
            if not msg:
                continue
            frm = msg.get("from") or {}
            if frm.get("id") == bot_id:
                continue  # never queue our own messages
            chat = msg.get("chat") or {}
            queued.append((frm.get("id"), chat.get("id"), chat.get("type"),
                           bool(frm.get("is_bot"))))
            last_queued = u["update_id"]
            lines.append(json.dumps({
                "update_id": u["update_id"],
                "chat_id": chat.get("id"),
                "chat_type": chat.get("type"),
                "from_id": frm.get("id"),
                "from_username": frm.get("username"),
                "from_is_bot": bool(frm.get("is_bot")),
                "text": msg.get("text") or "",
                "msg_type": msg_type(msg),
                "date": msg.get("date"),
            }, ensure_ascii=False))
        if lines:
            with open(INBOX_PATH, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            inbox_lines += len(lines)
            note_known_chats([chat_id for _, chat_id, _, _ in queued])
            oid = owner_id()
            for from_id, chat_id, chat_type, from_is_bot in queued:
                if chat_id is not None and not from_is_bot:
                    reset_bot_streak(chat_id)
                if oid is not None and from_id == oid and chat_id is not None:
                    # Off the polling thread: start_thinking makes two
                    # synchronous API calls (up to 35s each on a sick
                    # network) and must not delay the next getUpdates.
                    threading.Thread(
                        target=start_thinking, args=(chat_id, chat_type),
                        daemon=True,
                    ).start()
        save_json(OFFSET_PATH, {"offset": offset})


if __name__ == "__main__":
    main()
