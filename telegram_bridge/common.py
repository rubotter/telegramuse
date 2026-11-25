"""Shared helpers for the Telegram bridge kit (stdlib only, no deps).

Configuration is via environment variables:

  TELEGRAM_BOT_TOKEN  Bot token from @BotFather (required). Keep it in a
                      chmod-600 env file or a secret manager; never commit
                      it, never print it.
  TGBRIDGE_HOME       Base directory for state files and the inbox queue.
                      Defaults to the kit root (the parent of this package).

Layout under TGBRIDGE_HOME:
  inbox.jsonl         one JSON object per incoming message (listener -> agent)
  allowlist.json      {"owner_id": <int>, "owner_username": <str|null>}
  state/offset.json   getUpdates offset
  state/bot.json      cached getMe result
  state/thinking.json animated "thinking" placeholder state
  state/outbound-rate.json  bot-peer rate-limit stamps
"""
import json
import os
import urllib.request
from pathlib import Path

BRIDGE_HOME = Path(os.environ.get("TGBRIDGE_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BRIDGE_HOME / "state"
INBOX_PATH = BRIDGE_HOME / "inbox.jsonl"
ALLOWLIST_PATH = BRIDGE_HOME / "allowlist.json"
THINKING_PATH = STATE_DIR / "thinking.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


def _env_file_value(key: str) -> str:
    """Read one KEY=value from <BRIDGE_HOME>/bridge.env, if present.

    The listener gets the token from systemd's EnvironmentFile, but the
    agent-side sender (send_reply.py) is launched by the agent without
    that environment. Falling back to the same env file gives both
    processes one credential path.
    """
    try:
        with open(BRIDGE_HOME / "bridge.env", encoding="utf-8") as f:
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
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
