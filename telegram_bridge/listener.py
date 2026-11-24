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
    ALLOWLIST_PATH, INBOX_PATH, STATE_DIR, THINKING_PATH,
    api_call, load_json, save_json,
)

OFFSET_PATH = STATE_DIR / "offset.json"
BOT_PATH = STATE_DIR / "bot.json"

# --- Thinking placeholder style (customise freely) -----------------------
# Native "typing" chat action + a moon-phase cycle in the message text.
THINKING_FRAMES = [
    "🌑 思考中…", "🌒 思考中…", "🌓 思考中…", "🌔 思考中…",
    "🌕 思考中…", "🌖 思考中…", "🌗 思考中…", "🌘 思考中…",
]
THINKING_INTERVAL = 1.0   # seconds per frame (deadline-paced, see loop)
TYPING_REFRESH = 4.0      # sendChatAction typing lasts ~5s server-side
THINKING_TTL = 600.0      # placeholder auto-deleted after 10 minutes
_thinking_lock = threading.Lock()


def owner_id():
    return load_json(ALLOWLIST_PATH, {}).get("owner_id")


def start_thinking(chat_id):
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
            if time.time() - entry.get("last_action", 0) >= TYPING_REFRESH:
                try:
                    api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                except Exception:
                    pass
                entry["last_action"] = time.time()
                changed = True
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
                    merged.pop(k, None)
                for k, v in state.items():
                    if k in current:
                        merged[k] = v
                save_json(THINKING_PATH, merged)
        # Pace by deadline, not fixed sleep: an edit round-trip can take
        # most of the frame budget, so sleep only what is left.
        time.sleep(max(0.05, THINKING_INTERVAL - (time.time() - cycle_start)))


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)

    bot = load_json(BOT_PATH, None)
    if not bot:
        try:
            me = api_call("getMe")
        except Exception as e:
            print(f"getMe failed: {e} (check TELEGRAM_BOT_TOKEN)", flush=True)
            sys.exit(1)
        if not me.get("ok"):
            print(f"getMe failed: {me}", flush=True)
            sys.exit(1)
        bot = me["result"]
        save_json(BOT_PATH, bot)
    bot_id = bot.get("id")
    print(f"listener up: bot @{bot.get('username')} id={bot_id}", flush=True)

    threading.Thread(target=animation_loop, daemon=True).start()

    offset = load_json(OFFSET_PATH, {"offset": 0}).get("offset", 0)

    while True:
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

        updates = resp.get("result", [])
        if not updates:
            continue

        lines = []
        queued = []
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            msg = u.get("message")
            if not msg:
                continue
            frm = msg.get("from") or {}
            if frm.get("id") == bot_id:
                continue  # never queue our own messages
            chat = msg.get("chat") or {}
            queued.append((frm.get("id"), chat.get("id")))
            lines.append(json.dumps({
                "update_id": u["update_id"],
                "chat_id": chat.get("id"),
                "chat_type": chat.get("type"),
                "from_id": frm.get("id"),
                "from_username": frm.get("username"),
                "from_is_bot": bool(frm.get("is_bot")),
                "text": msg.get("text") or "",
                "date": msg.get("date"),
            }, ensure_ascii=False))
        if lines:
            with open(INBOX_PATH, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            oid = owner_id()
            if oid is not None:
                for from_id, chat_id in queued:
                    if from_id == oid and chat_id is not None:
                        start_thinking(chat_id)
        save_json(OFFSET_PATH, {"offset": offset})


if __name__ == "__main__":
    main()
