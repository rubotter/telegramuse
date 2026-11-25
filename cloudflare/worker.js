// Shared-state API gateway for the Telegram bridge kit.
// Cloudflare Worker with bindings: DB (D1), BUCKET (R2), ADMIN_TOKEN (secret).
//
// Security model: bots never see Cloudflare credentials. Each bot holds
// one Bearer token; the Worker hashes it (SHA-256) and looks up bot_id
// in api_tokens. A bot can only write its own definition/tasks and the
// R2 prefixes bots/<its bot_id>/ and shared/. Everything is readable
// by every authenticated bot. Admin endpoints need the ADMIN_TOKEN.

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const nowIso = () => new Date().toISOString();

async function authenticate(request, env) {
  const auth = request.headers.get("authorization") || "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  if (!m) return null;
  const hash = await sha256hex(m[1].trim());
  const row = await env.DB.prepare("SELECT bot_id FROM api_tokens WHERE token_hash = ?")
    .bind(hash)
    .first();
  return row ? row.bot_id : null;
}

function changeStmt(env, botId, kind, ref) {
  return env.DB.prepare("INSERT INTO changes (bot_id, kind, ref, changed_at) VALUES (?, ?, ?, ?)")
    .bind(botId, kind, ref || null, nowIso());
}

// Parse a JSON request body safely: invalid JSON and a literal `null`
// body both become {} instead of throwing on property access later.
async function jsonBody(request) {
  const body = await request.json().catch(() => null);
  return body && typeof body === "object" ? body : {};
}

function writableBy(path, botId) {
  return path.startsWith(`bots/${botId}/`) || path.startsWith("shared/");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const method = request.method;

    // ---------- admin: token issuance / revocation ----------
    if (path.startsWith("/v1/admin/")) {
      const admin = request.headers.get("x-admin-token") || "";
      if (!env.ADMIN_TOKEN || admin !== env.ADMIN_TOKEN) return json({ error: "forbidden" }, 403);
      if (path === "/v1/admin/tokens" && method === "POST") {
        const body = await jsonBody(request);
        if (!body.bot_id) return json({ error: "bot_id required" }, 400);
        const token = [...crypto.getRandomValues(new Uint8Array(24))]
          .map((b) => b.toString(16).padStart(2, "0")).join("");
        await env.DB.prepare("INSERT INTO api_tokens (token_hash, bot_id, created_at) VALUES (?, ?, ?)")
          .bind(await sha256hex(token), body.bot_id, nowIso())
          .run();
        // Plaintext is returned exactly once; only the hash is stored.
        return json({ bot_id: body.bot_id, token });
      }
      if (path === "/v1/admin/tokens" && method === "DELETE") {
        const body = await jsonBody(request);
        if (!body.bot_id) return json({ error: "bot_id required" }, 400);
        const r = await env.DB.prepare("DELETE FROM api_tokens WHERE bot_id = ?").bind(body.bot_id).run();
        return json({ bot_id: body.bot_id, revoked: r.meta.changes });
      }
      return json({ error: "not found" }, 404);
    }

    // ---------- everything below requires a bot token ----------
    const botId = await authenticate(request, env);
    if (!botId) return json({ error: "unauthorized" }, 401);

    // Own definition: upsert (write-own-only by construction).
    if (path === "/v1/me/definition" && method === "PUT") {
      const body = await jsonBody(request);
      if (typeof body.definition !== "string") return json({ error: "definition (string) required" }, 400);
      const ts = nowIso();
      // Business write + change-log entry in ONE D1 batch (transactional):
      // either both land or neither does, so peers never miss an update.
      await env.DB.batch([
        env.DB.prepare(
          `INSERT INTO bots (bot_id, display_name, owner, definition, version, updated_at)
           VALUES (?, ?, ?, ?, 1, ?)
           ON CONFLICT(bot_id) DO UPDATE SET
             display_name = excluded.display_name,
             owner = excluded.owner,
             definition = excluded.definition,
             version = bots.version + 1,
             updated_at = excluded.updated_at`
        ).bind(botId, body.display_name || botId, body.owner || null, body.definition, ts),
        changeStmt(env, botId, "definition", null),
      ]);
      const row = await env.DB.prepare("SELECT version, updated_at FROM bots WHERE bot_id = ?").bind(botId).first();
      return json({ bot_id: botId, ...row });
    }
    if (path === "/v1/me" && method === "GET") {
      const row = await env.DB.prepare("SELECT * FROM bots WHERE bot_id = ?").bind(botId).first();
      return json(row || { bot_id: botId, definition: null });
    }

    // All bots' definitions (roster sync).
    if (path === "/v1/bots" && method === "GET") {
      const { results } = await env.DB.prepare("SELECT * FROM bots ORDER BY bot_id").all();
      return json({ bots: results });
    }

    // Tasks: write own, read all.
    const taskMatch = path.match(/^\/v1\/me\/tasks\/(.+)$/);
    if (taskMatch && method === "PUT") {
      const key = decodeURIComponent(taskMatch[1]);
      const body = await jsonBody(request);
      const status = typeof body.status === "string" ? body.status : JSON.stringify(body.status ?? {});
      const ts = nowIso();
      await env.DB.batch([
        env.DB.prepare(
          `INSERT INTO tasks (bot_id, task_key, status, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(bot_id, task_key) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`
        ).bind(botId, key, status, ts),
        changeStmt(env, botId, "task", key),
      ]);
      return json({ bot_id: botId, task_key: key, updated_at: ts });
    }
    if (path === "/v1/tasks" && method === "GET") {
      const bot = url.searchParams.get("bot");
      const q = bot
        ? env.DB.prepare("SELECT * FROM tasks WHERE bot_id = ? ORDER BY task_key").bind(bot)
        : env.DB.prepare("SELECT * FROM tasks ORDER BY bot_id, task_key");
      const { results } = await q.all();
      return json({ tasks: results });
    }

    // Incremental changes feed.
    if (path === "/v1/changes" && method === "GET") {
      const since = parseInt(url.searchParams.get("since") || "0", 10) || 0;
      const { results } = await env.DB.prepare("SELECT * FROM changes WHERE seq > ? ORDER BY seq LIMIT 500").bind(since).all();
      const cursor = results.length ? results[results.length - 1].seq : since;
      return json({ changes: results, cursor });
    }

    // Files in R2: read any, write own prefix or shared/.
    const fileMatch = path.match(/^\/v1\/files\/(.+)$/);
    if (fileMatch) {
      const key = decodeURIComponent(fileMatch[1]);
      if (method === "GET") {
        const obj = await env.BUCKET.get(key);
        if (!obj) return json({ error: "not found" }, 404);
        return new Response(obj.body, {
          headers: { "content-type": obj.httpMetadata?.contentType || "application/octet-stream" },
        });
      }
      if (method === "PUT") {
        if (!writableBy(key, botId)) return json({ error: "may only write bots/<your bot_id>/ or shared/" }, 403);
        await env.BUCKET.put(key, request.body, {
          httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" },
        });
        // R2 and D1 cannot share a transaction; the change-log entry is
        // best-effort so a logging hiccup never makes a stored file look
        // failed. Peers can always reconcile via the file listing.
        let warning;
        try { await changeStmt(env, botId, "file", key).run(); } catch { warning = "change log entry failed"; }
        return json({ path: key, ok: true, ...(warning ? { warning } : {}) });
      }
      if (method === "DELETE") {
        if (!writableBy(key, botId)) return json({ error: "may only delete bots/<your bot_id>/ or shared/" }, 403);
        await env.BUCKET.delete(key);
        let warning;
        try { await changeStmt(env, botId, "file", key).run(); } catch { warning = "change log entry failed"; }
        return json({ path: key, deleted: true, ...(warning ? { warning } : {}) });
      }
      if (method === "HEAD" || (method === "GET" && url.searchParams.get("list") === "1")) {
        return json({ error: "use GET /v1/files?prefix= to list" }, 400);
      }
    }
    if (path === "/v1/files" && method === "GET") {
      const prefix = url.searchParams.get("prefix") || "";
      const cursor = url.searchParams.get("cursor") || undefined;
      const listed = await env.BUCKET.list({ prefix, limit: 500, cursor });
      return json({
        objects: listed.objects.map((o) => ({ key: o.key, size: o.size, uploaded: o.uploaded })),
        truncated: listed.truncated,
        cursor: listed.truncated ? listed.cursor : null,
      });
    }

    return json({ error: "not found" }, 404);
  },
};
