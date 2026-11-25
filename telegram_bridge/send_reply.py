#!/usr/bin/env python3
"""Send one Telegram reply for the bridge agent. The ONLY outbound path.

Usage:
  send_reply.py --chat-id <id> --text <text> [--peer-is-bot]
  send_reply.py --chat-id <id> --photo <path> [--text <caption>]
  send_reply.py --chat-id <id> --delete-thinking

Behaviour:
- Before every real send, the chat's animated "thinking" placeholder is
  deleted (see listener.py). --delete-thinking does only that cleanup,
  for rounds that end without a Telegram reply.
- Bot-peer guards: when the peer is another bot (--peer-is-bot), two
  independent caps apply to that chat:
  * rate: at most 60 outbound messages per rolling 60 seconds
    (exit code 3, prints RATE_LIMITED);
  * streak: at most 20 consecutive bot-to-bot messages without any
    human speaking in the chat (exit code 4, prints STREAK_LIMITED).
    A rate cap alone only slows a bot loop down; the streak cap is
    what guarantees it terminates. The listener resets the streak
    whenever a human message arrives in the chat.
  Human chats are not capped here; Telegram's own per-chat limit
  (~1 msg/s) still applies server-side.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telegram_bridge.common import (  # noqa: E402
    STATE_DIR, THINKING_PATH, api_call, api_call_multipart,
)

RATE_PATH = STATE_DIR / "outbound-rate.json"
STREAK_PATH = STATE_DIR / "bot-streak.json"
BOT_LIMIT = 60
WINDOW = 60.0
STREAK_LIMIT = 20


def load_state():
    try:
        with open(RATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(RATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, RATE_PATH)


def load_streak():
    try:
        with open(STREAK_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_streak(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(STREAK_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STREAK_PATH)


def chunk_text(text, limit=4000):
    """Split a long message into Telegram-sized chunks (API max 4096),
    preferring newline boundaries."""
    chunks = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


def clear_thinking(chat_id):
    """Delete the animated 'thinking' placeholder for this chat, if any."""
    try:
        with open(THINKING_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return
    entry = state.pop(str(chat_id), None)
    if entry is None:
        return
    tmp = str(THINKING_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, THINKING_PATH)
    try:
        api_call("deleteMessage", {"chat_id": chat_id, "message_id": entry["message_id"]})
    except Exception:
        pass  # already gone is fine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--text")
    ap.add_argument("--photo", help="Path to an image file to send (text becomes the caption)")
    ap.add_argument("--peer-is-bot", action="store_true")
    ap.add_argument("--delete-thinking", action="store_true",
                    help="Only delete the thinking placeholder for this chat, send nothing")
    args = ap.parse_args()
    if args.delete_thinking:
        clear_thinking(args.chat_id)
        print("THINKING_CLEARED")
        sys.exit(0)
    if not args.text and not args.photo:
        ap.error("either --text or --photo is required")

    key = str(args.chat_id)
    if args.peer_is_bot:
        # Check both caps BEFORE sending; budget is only recorded after
        # a successful send, so network failures don't burn quota or
        # trip the streak lockout.
        streaks = load_streak()
        if int(streaks.get(key, 0)) >= STREAK_LIMIT:
            print("STREAK_LIMITED")
            sys.exit(4)
        now = time.time()
        state = load_state()
        stamps = [t for t in state.get(key, []) if now - t < WINDOW]
        if len(stamps) >= BOT_LIMIT:
            print("RATE_LIMITED")
            sys.exit(3)

    clear_thinking(args.chat_id)

    ok = True
    last_resp = {}
    followups = []
    if args.photo:
        fields = {"chat_id": str(args.chat_id)}
        if args.text:
            if len(args.text) <= 1024:
                fields["caption"] = args.text
            else:
                fields["caption"] = args.text[:1024]
                followups = chunk_text(args.text[1024:])
        last_resp = api_call_multipart("sendPhoto", fields, "photo", args.photo)
        ok = bool(last_resp.get("ok"))
    else:
        followups = chunk_text(args.text)
    if ok:
        for chunk in followups:
            last_resp = api_call("sendMessage", {"chat_id": args.chat_id, "text": chunk})
            if not last_resp.get("ok"):
                ok = False
                break

    if args.peer_is_bot and ok:
        now = time.time()
        state = load_state()
        stamps = [t for t in state.get(key, []) if now - t < WINDOW]
        stamps.append(now)
        state[key] = stamps
        save_state(state)
        streaks = load_streak()
        streaks[key] = int(streaks.get(key, 0)) + 1
        save_streak(streaks)

    print(json.dumps(last_resp, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
