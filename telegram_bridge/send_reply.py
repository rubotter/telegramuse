#!/usr/bin/env python3
"""Send one Telegram reply for the bridge agent. The ONLY outbound path.

Usage:
  send_reply.py --chat-id <id> --text <text> [--peer-is-bot]
  send_reply.py --chat-id <id> --photo <path> [--text <caption>]
  send_reply.py --chat-id <id> --delete-thinking
  send_reply.py --chat-id <id> --text <text> --force   (unknown chat)

Behaviour:
- Before every real send, the chat's animated "thinking" placeholder is
  deleted (see listener.py). --delete-thinking does only that cleanup,
  for rounds that end without a Telegram reply.
- Known-chat guard: sends only go to chats the listener has actually
  received a message from (state/known-chats.json, maintained by
  listener.py). An unknown chat is refused (exit code 5, prints
  UNKNOWN_CHAT) unless --force is given. This is defence in depth: the
  worker rules live in the prompt, but the one outbound script should
  not be usable to message arbitrary chats (or leak local files as
  photos) just because a queued message talked the worker into it.
  If known-chats.json does not exist yet (listener never ran), sends
  are allowed so a fresh install is not wedged.
- Bot-peer guards: when the peer is another bot (--peer-is-bot), two
  independent caps apply to that chat:
  * rate: at most 60 outbound MESSAGES per rolling 60 seconds
    (exit code 3, prints RATE_LIMITED);
  * streak: at most 20 consecutive bot-to-bot messages without any
    human speaking in the chat (exit code 4, prints STREAK_LIMITED).
    A rate cap alone only slows a bot loop down; the streak cap is
    what guarantees it terminates. The listener resets the streak
    whenever a human message arrives in the chat.
  Both caps count actual Telegram messages, not calls: one invocation
  that splits a long text into 5 chunks consumes 5 of each budget, and
  a partially failed send still accounts for the messages that did go
  out. Check + send + accounting run under one file lock so concurrent
  send_reply processes cannot slip past the caps together.
  Human chats are not capped here; Telegram's own per-chat limit
  (~1 msg/s) still applies server-side.
"""
import argparse
import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telegram_bridge.common import (  # noqa: E402
    KNOWN_CHATS_PATH, STATE_DIR, THINKING_PATH, api_call, api_call_multipart,
    load_json, save_json,
)

RATE_PATH = STATE_DIR / "outbound-rate.json"
STREAK_PATH = STATE_DIR / "bot-streak.json"
LOCK_PATH = STATE_DIR / "outbound.lock"
BOT_LIMIT = 60
WINDOW = 60.0
STREAK_LIMIT = 20


@contextlib.contextmanager
def outbound_lock():
    """Serialise bot-peer check/send/account across processes."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
    state = load_json(THINKING_PATH, {})
    if not isinstance(state, dict):
        return
    entry = state.pop(str(chat_id), None)
    if entry is None:
        return
    # save_json writes via a per-writer unique tmp name: the listener's
    # animation thread writes this same file about once a second, and a
    # shared tmp name used to corrupt it (or crash this cleanup, and
    # with it the reply that follows).
    save_json(THINKING_PATH, state)
    try:
        api_call("deleteMessage", {"chat_id": chat_id, "message_id": entry["message_id"]})
    except Exception:
        pass  # already gone is fine


def known_chat(chat_id):
    """True when the listener has seen this chat (or no registry yet)."""
    if not KNOWN_CHATS_PATH.exists():
        return True
    known = load_json(KNOWN_CHATS_PATH, [])
    if not isinstance(known, list):
        return True
    return str(chat_id) in {str(c) for c in known}


def do_send(args):
    """Send the photo/text. Returns (ok, last_resp, messages_sent)."""
    sent = 0
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
        if not last_resp.get("ok"):
            return False, last_resp, 0
        sent = 1
    else:
        followups = chunk_text(args.text)
    for chunk in followups:
        last_resp = api_call("sendMessage", {"chat_id": args.chat_id, "text": chunk})
        if not last_resp.get("ok"):
            return False, last_resp, sent
        sent += 1
    return True, last_resp, sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--text")
    ap.add_argument("--photo", help="Path to an image file to send (text becomes the caption)")
    ap.add_argument("--peer-is-bot", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Send even if the chat is not in known-chats.json")
    ap.add_argument("--delete-thinking", action="store_true",
                    help="Only delete the thinking placeholder for this chat, send nothing")
    args = ap.parse_args()
    if args.delete_thinking:
        clear_thinking(args.chat_id)
        print("THINKING_CLEARED")
        sys.exit(0)
    if not args.text and not args.photo:
        ap.error("either --text or --photo is required")

    if not args.force and not known_chat(args.chat_id):
        print("UNKNOWN_CHAT")
        sys.exit(5)

    key = str(args.chat_id)
    if not args.peer_is_bot:
        clear_thinking(args.chat_id)
        ok, last_resp, _sent = do_send(args)
        print(json.dumps(last_resp, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    # Bot peer: cap check, send and accounting under one lock, so two
    # concurrent senders cannot both pass the check before either
    # records. Accounting is per actual message sent (chunks included)
    # and happens even when a later chunk fails — those messages are
    # out and must count against both caps.
    with outbound_lock():
        streaks = load_json(STREAK_PATH, {})
        if not isinstance(streaks, dict):
            streaks = {}
        if int(streaks.get(key, 0)) >= STREAK_LIMIT:
            print("STREAK_LIMITED")
            sys.exit(4)
        now = time.time()
        state = load_json(RATE_PATH, {})
        if not isinstance(state, dict):
            state = {}
        stamps = [t for t in state.get(key, []) if now - t < WINDOW]
        if len(stamps) >= BOT_LIMIT:
            print("RATE_LIMITED")
            sys.exit(3)

        clear_thinking(args.chat_id)
        ok, last_resp, sent = do_send(args)

        if sent:
            now = time.time()
            state = load_json(RATE_PATH, {})
            if not isinstance(state, dict):
                state = {}
            stamps = [t for t in state.get(key, []) if now - t < WINDOW]
            stamps.extend([now] * sent)
            state[key] = stamps
            save_json(RATE_PATH, state)
            streaks = load_json(STREAK_PATH, {})
            if not isinstance(streaks, dict):
                streaks = {}
            streaks[key] = int(streaks.get(key, 0)) + sent
            save_json(STREAK_PATH, streaks)

    print(json.dumps(last_resp, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
