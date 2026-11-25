#!/usr/bin/env python3
"""Bot-side client for the shared-state API (Cloudflare Worker + D1 + R2).

Stdlib only. Config via environment:
  SHARED_STATE_API    e.g. https://bot-shared-state.<you>.workers.dev
  SHARED_STATE_TOKEN  this bot's own Bearer token (issued by the admin)

Library use:
  from shared_state import SharedState
  st = SharedState.from_env()
  st.push_definition(open('BOT_SOUL.md').read(), display_name='小哈', owner='YUHUI')
  st.sync_roster('GROUP_ROSTER.md')      # refresh local roster cache from DB
  st.push_task('poa-mining', {'epoch': 124, 'status': 'answered'})

CLI:
  python3 shared_state.py push-definition --soul BOT_SOUL.md --name 小哈 --owner YUHUI
  python3 shared_state.py sync-roster --out GROUP_ROSTER.md
  python3 shared_state.py changes [--since N]
  python3 shared_state.py upload <local-file> <remote-path>
  python3 shared_state.py download <remote-path> <local-file>
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

CURSOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".shared-state-cursor")


class SharedState:
    def __init__(self, api_base, token):
        self.api = api_base.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls):
        api = os.environ.get("SHARED_STATE_API")
        token = os.environ.get("SHARED_STATE_TOKEN")
        if not api or not token:
            raise SystemExit("Set SHARED_STATE_API and SHARED_STATE_TOKEN")
        return cls(api, token)

    def _req(self, method, path, body=None, raw_body=False, raw_response=False, content_type=None):
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            if raw_body:
                data = body if isinstance(body, bytes) else body.encode()
                headers["Content-Type"] = content_type or "application/octet-stream"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
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

    def _load_cursors(self):
        # Cursors are stored per API base: pointing the same client at a
        # different backend (test vs production) must never reuse a cursor.
        try:
            with open(CURSOR_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def stored_cursor(self):
        return int(self._load_cursors().get(self.api, 0))

    def poll_changes(self):
        """Fetch changes since the stored cursor WITHOUT committing it.

        Returns (changes, new_cursor). Only call ack_changes(new_cursor)
        after the changes have been fully processed (e.g. roster cache
        rewritten); if processing fails, the next poll returns the same
        changes again instead of losing them.
        """
        result = self.get_changes(self.stored_cursor())
        return result["changes"], result["cursor"]

    def ack_changes(self, cursor):
        cursors = self._load_cursors()
        cursors[self.api] = int(cursor)
        tmp = CURSOR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cursors, f)
        os.replace(tmp, CURSOR_FILE)

    # ---- files (R2 via Worker) ----
    def upload_file(self, local_path, remote_path):
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
    p = sub.add_parser("upload")
    p.add_argument("local")
    p.add_argument("remote")
    p = sub.add_parser("download")
    p.add_argument("remote")
    p.add_argument("local")
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
    elif args.cmd == "upload":
        print(json.dumps(st.upload_file(args.local, args.remote), ensure_ascii=False))
    elif args.cmd == "download":
        n = st.download_file(args.remote, args.local)
        print(f"downloaded {n} bytes -> {args.local}")


if __name__ == "__main__":
    main()
