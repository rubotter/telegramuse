#!/usr/bin/env python3
"""Bot-side client for the shared-state API (Cloudflare Worker + D1 + R2).

Stdlib only. Configuration, first hit wins per key:
  1. environment variables SHARED_STATE_API / SHARED_STATE_TOKEN
  2. the same keys in the kit root's bridge.env (chmod 600) — this is
     the path that works for agent workers woken by a hook: they are
     NOT interactive shells and never see an `export` typed in a
     terminal, but they can always read the env file.
  SHARED_STATE_API    e.g. https://bot-shared-state.<you>.workers.dev
  SHARED_STATE_TOKEN  this bot's own Bearer token (issued by the admin)

The API base must be https (plain http is refused, localhost excepted
for development): the Bearer token must never travel in cleartext, and
redirects are refused outright — a redirecting API address would
otherwise carry the Authorization header to another host.

Library use:
  from shared_state import SharedState
  st = SharedState.from_env()
  st.push_definition(open('BOT_SOUL.md').read(), display_name='小哈', owner='YUHUI')
  st.sync_roster('rosters/cloud-roster.md')  # refresh roster cache from DB
  st.push_task('poa-mining', {'epoch': 124, 'status': 'answered'})

CLI:
  python3 shared_state.py push-definition --soul BOT_SOUL.md --name 小哈 --owner YUHUI
  python3 shared_state.py sync-roster --out rosters/cloud-roster.md
  python3 shared_state.py changes [--since N]
  python3 shared_state.py poll          # fetch un-acked changes, print new cursor
  python3 shared_state.py ack <cursor>  # commit cursor after processing
  python3 shared_state.py push-task <key> --status-json '{"epoch": 124}'
  python3 shared_state.py tasks [--bot <bot_id>]
  python3 shared_state.py bots
  python3 shared_state.py upload <local-file> <remote-path>
  python3 shared_state.py download <remote-path> <local-file>
  python3 shared_state.py list-files [--prefix shared/]

Note: sync-roster writes a GENERATED cache. Point it at an ignored
cache path (e.g. rosters/cloud-roster.md), never at a hand-maintained
roster file — it overwrites the target wholesale.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

CURSOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".shared-state-cursor")
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # refuse larger files with a clear error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused: SHARED_STATE_API must not redirect "
                           "(the Bearer token would be forwarded to the target)")


def _bridge_env_value(key):
    try:
        with open(os.path.join(KIT_ROOT, "bridge.env"), encoding="utf-8") as f:
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


def _check_api_base(api):
    parsed = urllib.parse.urlparse(api)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise ValueError(f"SHARED_STATE_API must be https (got {api!r}); "
                     "the Bearer token must never travel in cleartext")


class SharedState:
    def __init__(self, api_base, token):
        _check_api_base(api_base)
        self.api = api_base.rstrip("/")
        self.token = token
        self.window_truncated = False

    @classmethod
    def from_env(cls):
        api = os.environ.get("SHARED_STATE_API") or _bridge_env_value("SHARED_STATE_API")
        token = os.environ.get("SHARED_STATE_TOKEN") or _bridge_env_value("SHARED_STATE_TOKEN")
        if not api or not token:
            raise SystemExit("Set SHARED_STATE_API and SHARED_STATE_TOKEN "
                             "(environment, or the kit root's bridge.env)")
        return cls(api, token)

    def _req(self, method, path, body=None, raw_body=False, raw_response=False, content_type=None):
        data = None
        # A non-default, deployment-neutral UA: Cloudflare's edge
        # rejects the Python-urllib signature in front of workers.dev
        # with error 1010.
        headers = {"Authorization": f"Bearer {self.token}",
                   "User-Agent": "shared-state-bridge/1.0",
                   "Accept": "application/json"}
        if body is not None:
            if raw_body:
                data = body if isinstance(body, bytes) else body.encode()
                headers["Content-Type"] = content_type or "application/octet-stream"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as resp:
                payload = resp.read()
                if raw_response:
                    return payload
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None

    # ---- definitions & roster ----
    def push_definition(self, definition, display_name=None, owner=None):
        return self._req("PUT", "/v1/me/definition",
                         {"definition": definition, "display_name": display_name, "owner": owner})

    def get_bots(self):
        return self._req("GET", "/v1/bots")["bots"]

    def sync_roster(self, out_path, group_title="群成员名册（云端同步）"):
        bots = self.get_bots()
        lines = [f"# {group_title}", "",
                 "> 本文件由 shared_state.py 从公共数据库生成，是缓存副本，",
                 "> 勿手工编辑；定义以各 Bot 在库中的版本为准。", ""]
        for b in bots:
            lines += [f"## {b['display_name']}（@{b['bot_id']}）",
                      f"- 机主：{b.get('owner') or '未登记'}",
                      f"- 定义版本：v{b['version']}，更新于 {b['updated_at']}", "",
                      b["definition"].strip(), "", "---", ""]
        # Atomic write (tmp + replace, unique tmp name): a crash
        # mid-write must not leave a truncated roster cache behind.
        directory = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{out_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, out_path)
        return len(bots)

    # ---- tasks ----
    def push_task(self, key, status):
        return self._req("PUT", f"/v1/me/tasks/{urllib.parse.quote(key, safe='')}", {"status": status})

    def get_tasks(self, bot=None):
        q = f"?bot={urllib.parse.quote(bot)}" if bot else ""
        return self._req("GET", f"/v1/tasks{q}")["tasks"]

    # ---- incremental changes ----
    def get_changes(self, since=0):
        return self._req("GET", f"/v1/changes?since={int(since)}")

    @property
    def _cursor_key(self):
        # Cursors are stored per (API base, bot identity): two bots
        # sharing one client install against the same backend must not
        # share a cursor either, or one bot's ack would skip the other's
        # sync. The token fingerprint stands in for the bot identity.
        import hashlib

        fp = hashlib.sha256(self.token.encode()).hexdigest()[:12]
        return f"{self.api}|{fp}"

    def _load_cursors(self):
        try:
            with open(CURSOR_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def stored_cursor(self):
        try:
            return int(self._load_cursors().get(self._cursor_key, 0))
        except (TypeError, ValueError):
            return 0  # corrupt cursor value: rescan rather than crash

    def poll_changes(self):
        """Fetch changes since the stored cursor WITHOUT committing it.

        Returns (changes, new_cursor). Only call ack_changes(new_cursor)
        after the changes have been fully processed (e.g. roster cache
        rewritten); if processing fails, the next poll returns the same
        changes again instead of losing them.

        If the stored cursor fell behind the server's retention window,
        some changes were pruned before delivery: the gateway flags
        that (truncated) and this method sets self.window_truncated.
        In that case do a full re-sync (sync_roster) BEFORE ack-ing —
        ack-ing straight past the gap would silently drop updates.
        """
        result = self.get_changes(self.stored_cursor())
        self.window_truncated = bool(result.get("truncated"))
        return result["changes"], result["cursor"]

    def ack_changes(self, cursor):
        cursors = self._load_cursors()
        cursors[self._cursor_key] = int(cursor)
        tmp = f"{CURSOR_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(cursors, f)
        os.replace(tmp, CURSOR_FILE)

    # ---- files (R2 via Worker) ----
    def upload_file(self, local_path, remote_path):
        # The file is read into memory whole; refuse oversize files
        # with a clear error instead of OOM-ing a small machine.
        size = os.path.getsize(local_path)
        if size > MAX_UPLOAD_BYTES:
            raise RuntimeError(
                f"{local_path} is {size} bytes; upload limit is {MAX_UPLOAD_BYTES} "
                "bytes (MAX_UPLOAD_BYTES in shared_state.py)")
        with open(local_path, "rb") as f:
            data = f.read()
        return self._req("PUT", f"/v1/files/{urllib.parse.quote(remote_path)}",
                         body=data, raw_body=True)

    def download_file(self, remote_path, local_path):
        data = self._req("GET", f"/v1/files/{urllib.parse.quote(remote_path)}", raw_response=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return len(data)

    def list_files(self, prefix=""):
        """List all objects under a prefix, following pagination cursors."""
        objects, cursor = [], None
        while True:
            q = f"?prefix={urllib.parse.quote(prefix)}"
            if cursor:
                q += f"&cursor={urllib.parse.quote(cursor)}"
            page = self._req("GET", f"/v1/files{q}")
            objects.extend(page["objects"])
            if not page.get("truncated"):
                return objects
            cursor = page["cursor"]


def main():
    ap = argparse.ArgumentParser(description="Shared-state client for bridge bots")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push-definition")
    p.add_argument("--soul", required=True)
    p.add_argument("--name")
    p.add_argument("--owner")
    p = sub.add_parser("sync-roster")
    p.add_argument("--out", required=True)
    p = sub.add_parser("changes")
    p.add_argument("--since", type=int, default=0)
    sub.add_parser("poll")
    p = sub.add_parser("ack")
    p.add_argument("cursor", type=int)
    p = sub.add_parser("push-task")
    p.add_argument("key")
    p.add_argument("--status-json", required=True)
    p = sub.add_parser("tasks")
    p.add_argument("--bot")
    sub.add_parser("bots")
    p = sub.add_parser("upload")
    p.add_argument("local")
    p.add_argument("remote")
    p = sub.add_parser("download")
    p.add_argument("remote")
    p.add_argument("local")
    p = sub.add_parser("list-files")
    p.add_argument("--prefix", default="")
    args = ap.parse_args()
    st = SharedState.from_env()
    if args.cmd == "push-definition":
        text = open(args.soul, encoding="utf-8").read()
        print(json.dumps(st.push_definition(text, args.name, args.owner), ensure_ascii=False))
    elif args.cmd == "sync-roster":
        n = st.sync_roster(args.out)
        print(f"roster synced: {n} bot(s) -> {args.out}")
    elif args.cmd == "changes":
        print(json.dumps(st.get_changes(args.since), ensure_ascii=False, indent=1))
    elif args.cmd == "poll":
        changes, cursor = st.poll_changes()
        print(json.dumps({"changes": changes, "cursor": cursor},
                         ensure_ascii=False, indent=1))
        if st.window_truncated:
            print("WARNING: cursor is behind the server retention window; "
                  "some changes were pruned before delivery. Do a full "
                  "re-sync (sync-roster) before ack-ing this cursor.",
                  file=sys.stderr)
    elif args.cmd == "ack":
        st.ack_changes(args.cursor)
        print(f"acked cursor {args.cursor}")
    elif args.cmd == "push-task":
        print(json.dumps(st.push_task(args.key, json.loads(args.status_json)),
                         ensure_ascii=False))
    elif args.cmd == "tasks":
        print(json.dumps(st.get_tasks(args.bot), ensure_ascii=False, indent=1))
    elif args.cmd == "bots":
        print(json.dumps(st.get_bots(), ensure_ascii=False, indent=1))
    elif args.cmd == "upload":
        print(json.dumps(st.upload_file(args.local, args.remote), ensure_ascii=False))
    elif args.cmd == "download":
        n = st.download_file(args.remote, args.local)
        print(f"downloaded {n} bytes -> {args.local}")
    elif args.cmd == "list-files":
        print(json.dumps(st.list_files(args.prefix), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
