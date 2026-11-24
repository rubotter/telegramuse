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
- Bot-peer guard: when the peer is another bot (--peer-is-bot), at most
  60 outbound messages per rolling 60 seconds to that chat. Beyond that
  the send is dropped (exit code 3, prints RATE_LIMITED) so two bots can
  never talk forever. Human chats are not capped here; Telegram's own
  per-chat limit (~1 msg/s) still applies server-side.
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
BOT_LIMIT = 60
WINDOW = 60.0


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

    if args.peer_is_bot:
        now = time.time()
        state = load_state()
        key = str(args.chat_id)
        stamps = [t for t in state.get(key, []) if now - t < WINDOW]
        if len(stamps) >= BOT_LIMIT:
            state[key] = stamps
            save_state(state)
            print("RATE_LIMITED")
            sys.exit(3)
        stamps.append(now)
        state[key] = stamps
        save_state(state)

    clear_thinking(args.chat_id)

    if args.photo:
        fields = {"chat_id": str(args.chat_id)}
        if args.text:
            fields["caption"] = args.text
        resp = api_call_multipart("sendPhoto", fields, "photo", args.photo)
    else:
        resp = api_call("sendMessage", {"chat_id": args.chat_id, "text": args.text})
    print(json.dumps(resp, ensure_ascii=False))
    sys.exit(0 if resp.get("ok") else 1)


if __name__ == "__main__":
    main()
