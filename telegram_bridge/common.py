"""Shared helpers for the Telegram bridge kit (stdlib only, no deps).

Configuration is via environment variables:

  TELEGRAM_BOT_TOKEN  Bot token from @BotFather (required). Keep it in a
                      chmod-600 env file or a secret manager; never commit
                      it, never print it.
  TGBRIDGE_HOME       Base directory for state files and the inbox queue.
                      Resolution order (first hit wins): the environment
                      variable, then TGBRIDGE_HOME inside the kit root's
                      bridge.env, then the kit root itself (the parent of
                      this package). Every component (listener, sender,
                      hooks, self-heal) resolves it the SAME way, so
                      setting it in bridge.env — as bridge.env.example
                      suggests — can no longer split the bridge in two.

Layout under TGBRIDGE_HOME:
  inbox.jsonl         one JSON object per incoming message (listener -> agent)
  allowlist.json      {"owner_id": <int>, "owner_username": <str|null>}
  bridge.env          env file (token, optional TGBRIDGE_HOME); the
                      listener reads the kit root's copy via systemd
  state/offset.json   getUpdates offset
  state/bot.json      cached getMe result
  state/thinking.json animated "thinking" placeholder state
  state/outbound-rate.json  bot-peer rate-limit stamps
  state/known-chats.json    chat ids the listener has seen (send guard)
"""
import json
import os
import urllib.request
import uuid
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent


def _env_file_value_at(env_path: Path, key: str) -> str:
    """Read one KEY=value from a specific env file ('' when absent)."""
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _resolve_home() -> Path:
    env = os.environ.get("TGBRIDGE_HOME", "").strip()
    if env:
        return Path(env)
    # bridge.env sits in the kit root (systemd's EnvironmentFile points
    # there); a TGBRIDGE_HOME set inside it must be honoured by every
    # component, not just the listener.
    from_file = _env_file_value_at(KIT_ROOT / "bridge.env", "TGBRIDGE_HOME")
    if from_file:
        return Path(from_file)
    return KIT_ROOT


BRIDGE_HOME = _resolve_home()
STATE_DIR = BRIDGE_HOME / "state"
INBOX_PATH = BRIDGE_HOME / "inbox.jsonl"
ALLOWLIST_PATH = BRIDGE_HOME / "allowlist.json"
THINKING_PATH = STATE_DIR / "thinking.json"
KNOWN_CHATS_PATH = STATE_DIR / "known-chats.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


def _env_file_value(key: str) -> str:
    """Read one KEY=value from bridge.env, if present.

    The listener gets the token from systemd's EnvironmentFile, but the
    agent-side sender (send_reply.py) is launched by the agent without
    that environment. Falling back to the same env file gives both
    processes one credential path. The resolved home's bridge.env wins;
    the kit root's copy (the one systemd actually loads) is the fallback,
    which matters when TGBRIDGE_HOME points elsewhere.
    """
    for base in (BRIDGE_HOME, KIT_ROOT):
        value = _env_file_value_at(base / "bridge.env", key)
        if value:
            return value
    return ""


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        token = _env_file_value("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set (checked environment and bridge.env)"
        )
    return token


def token_fingerprint() -> str:
    """Short stable fingerprint of the current token (never the token).

    Used to detect token/bot changes against cached state such as
    state/bot.json: if the fingerprint differs, the cache is stale.
    """
    import hashlib

    return hashlib.sha256(_token().encode()).hexdigest()[:16]


def api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{_token()}/{method}"


def _read_json(resp) -> dict:
    return json.loads(resp.read().decode("utf-8"))


def api_call(method: str, params: dict | None = None, timeout: float = 35) -> dict:
    data = None
    headers = {}
    if params is not None:
        data = json.dumps(params).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        api_url(method), data=data, headers=headers,
        method="POST" if data else "GET",
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as resp:
        return _read_json(resp)


def api_call_multipart(method: str, fields: dict, file_field: str,
                       file_path: str, timeout: float = 60) -> dict:
    """POST multipart/form-data with one file part (for sendPhoto etc.)."""
    import mimetypes
    import uuid

    boundary = "----tgbridge" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    filename = str(file_path).rsplit("/", 1)[-1]
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        content = f.read()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {ctype}\r\n\r\n".encode()
    body += content
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        api_url(method),
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as resp:
        return _read_json(resp)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name per writer: several processes (listener animation
    # thread, send_reply, hooks) write the same state files, and a
    # shared fixed ".tmp" name lets two writers interleave into one
    # truncated file that os.replace then installs as corrupt JSON —
    # or makes the replace itself fail when the other writer moved the
    # tmp file first.
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
