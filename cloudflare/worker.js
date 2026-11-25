// Shared-state API gateway for the Telegram bridge kit.
// Cloudflare Worker with bindings: DB (D1), BUCKET (R2), ADMIN_TOKEN (secret).
//
// Security model: bots never see Cloudflare credentials. Each bot holds
// one Bearer token; the Worker hashes it (SHA-256) and looks up bot_id
// in api_tokens. A bot can only write its own definition/tasks and the
// R2 prefixes bots/<its bot_id>/ and shared/. Everything — including
// other bots' bots/<bot_id>/ files — is READABLE by every authenticated
// bot: bots/<bot_id>/ is a write-protected area, NOT a private one, so
// never store secrets or sensitive files there. Admin endpoints need
// the ADMIN_TOKEN.
//
// Resource limits (shared free-tier quotas are communal — one leaked
// or runaway token must not be able to drain them for everyone):
// definition <= 256 KB, task status <= 64 KB, task key <= 200 chars,
// list endpoints capped (see LIST_LIMIT, with a truncated flag).

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Constant-time equality for secrets: compare SHA-256 digests (fixed
// length, no early exit) instead of the raw strings.
async function secretEquals(a, b) {
  const [ha, hb] = await Promise.all([sha256hex(a), sha256hex(b)]);
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha.charCodeAt(i) ^ hb.charCodeAt(i);
  return diff === 0;
}

const nowIso = () => new Date().toISOString();

const MAX_DEFINITION_BYTES = 256 * 1024;
const MAX_TASK_STATUS_BYTES = 64 * 1024;
const MAX_TASK_KEY_CHARS = 200;
const MAX_NAME_CHARS = 200;
const LIST_LIMIT = 1000;
const BOT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

const byteLen = (s) => new TextEncoder().encode(s).length;

// decodeURIComponent on unchecked input throws URIError on malformed
// escapes (e.g. %E0%A4%A); map that to null so the caller can answer a
// clean JSON 400 instead of a bare 500 page.
function safeDecode(s) {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}

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

function changeStmt(env, botId, kind, ref, op) {
  return env.DB.prepare("INSERT INTO changes (bot_id, kind, ref, op, changed_at) VALUES (?, ?, ?, ?, ?)")
    .bind(botId, kind, ref || null, op || null, nowIso());
}

// Retention: keep only the newest CHANGE_KEEP change-log rows. Pruning
// rides along inside EVERY write path (definitions, tasks, file
// put/delete); cursors older than the retained window are told so via
// the changes feed's truncated flag and should do a full re-sync.
const CHANGE_KEEP = 5000;
function pruneStmt(env) {
  return env.DB.prepare(
    "DELETE FROM changes WHERE seq <= (SELECT MAX(seq) FROM changes) - ?"
  ).bind(CHANGE_KEEP);
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

async function handle(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method;

  // ---------- admin: token issuance / revocation ----------
  if (path.startsWith("/v1/admin/")) {
    const admin = request.headers.get("x-admin-token") || "";
    if (!env.ADMIN_TOKEN || !(await secretEquals(admin, env.ADMIN_TOKEN))) {
      return json({ error: "forbidden" }, 403);
    }
    if (path === "/v1/admin/tokens" && (method === "POST" || method === "DELETE")) {
      const body = await jsonBody(request);
      // bot_id becomes an R2 prefix and a roster identity: restrict it
      // to a safe alphabet. A bot_id containing "/" would warp the
      // prefix namespace (bot "a/b" living under bot "a"'s prefix),
      // and a non-string would 500 on bind.
      if (typeof body.bot_id !== "string" || !BOT_ID_RE.test(body.bot_id)) {
        return json({ error: "bot_id must match ^[A-Za-z0-9_-]{1,64}$" }, 400);
      }
      if (method === "POST") {
        const token = [...crypto.getRandomValues(new Uint8Array(24))]
          .map((b) => b.toString(16).padStart(2, "0")).join("");
        await env.DB.prepare("INSERT INTO api_tokens (token_hash, bot_id, created_at) VALUES (?, ?, ?)")
          .bind(await sha256hex(token), body.bot_id, nowIso())
          .run();
        // Plaintext is returned exactly once; only the hash is stored.
        return json({ bot_id: body.bot_id, token });
      }
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
    if (byteLen(body.definition) > MAX_DEFINITION_BYTES) return json({ error: "definition too large" }, 413);
    for (const field of ["display_name", "owner"]) {
      if (body[field] != null && (typeof body[field] !== "string" || body[field].length > MAX_NAME_CHARS)) {
        return json({ error: `${field} must be a string of at most ${MAX_NAME_CHARS} chars` }, 400);
      }
    }
    const ts = nowIso();
    // Partial updates keep stored display_name/owner when the fields
    // are omitted. Resolve the effective values BEFORE the upsert:
    // folding the fallback into the INSERT side (COALESCE(?, botId))
    // poisons excluded.display_name with a non-null value, so the
    // ON CONFLICT COALESCE would always pick it and silently reset a
    // stored display name to the bot_id on every name-less push.
    const existing = await env.DB.prepare("SELECT display_name, owner FROM bots WHERE bot_id = ?")
      .bind(botId)
      .first();
    const displayName = body.display_name ?? (existing ? existing.display_name : botId);
    const owner = body.owner ?? (existing ? existing.owner : null);
    // Business write + change-log entry + prune in ONE D1 batch
    // (transactional): peers never miss an update.
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
      ).bind(botId, displayName, owner, body.definition, ts),
      changeStmt(env, botId, "definition", null, "update"),
      pruneStmt(env),
    ]);
    const row = await env.DB.prepare("SELECT version, updated_at FROM bots WHERE bot_id = ?").bind(botId).first();
    return json({ bot_id: botId, ...row });
  }
  if (path === "/v1/me" && method === "GET") {
    const row = await env.DB.prepare("SELECT * FROM bots WHERE bot_id = ?").bind(botId).first();
    return json(row || { bot_id: botId, definition: null });
  }

  // All bots' definitions (roster sync). Capped: fetch one extra row
  // to learn whether the result was truncated.
  if (path === "/v1/bots" && method === "GET") {
    const { results } = await env.DB.prepare("SELECT * FROM bots ORDER BY bot_id LIMIT ?")
      .bind(LIST_LIMIT + 1)
      .all();
    const truncated = results.length > LIST_LIMIT;
    return json({ bots: truncated ? results.slice(0, LIST_LIMIT) : results, truncated });
  }

  // Tasks: write own, read all.
  const taskMatch = path.match(/^\/v1\/me\/tasks\/(.+)$/);
  if (taskMatch && method === "PUT") {
    const key = safeDecode(taskMatch[1]);
    if (key === null || key.length === 0) return json({ error: "bad task key" }, 400);
    if (key.length > MAX_TASK_KEY_CHARS) return json({ error: "task key too long" }, 400);
    const body = await jsonBody(request);
    const status = typeof body.status === "string" ? body.status : JSON.stringify(body.status ?? {});
    if (byteLen(status) > MAX_TASK_STATUS_BYTES) return json({ error: "task status too large" }, 413);
    const ts = nowIso();
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO tasks (bot_id, task_key, status, updated_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(bot_id, task_key) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`
      ).bind(botId, key, status, ts),
      changeStmt(env, botId, "task", key, "update"),
      pruneStmt(env),
    ]);
    return json({ bot_id: botId, task_key: key, updated_at: ts });
  }
  if (path === "/v1/tasks" && method === "GET") {
    const bot = url.searchParams.get("bot");
    const q = bot
      ? env.DB.prepare("SELECT * FROM tasks WHERE bot_id = ? ORDER BY task_key LIMIT ?").bind(bot, LIST_LIMIT + 1)
      : env.DB.prepare("SELECT * FROM tasks ORDER BY bot_id, task_key LIMIT ?").bind(LIST_LIMIT + 1);
    const { results } = await q.all();
    const truncated = results.length > LIST_LIMIT;
    return json({ tasks: truncated ? results.slice(0, LIST_LIMIT) : results, truncated });
  }

  // Incremental changes feed. oldest_seq + truncated tell a client
  // whose cursor fell behind the retention window that increments
  // were lost, so it can do a full re-sync instead of silently
  // ack-ing past the gap.
  if (path === "/v1/changes" && method === "GET") {
    const since = parseInt(url.searchParams.get("since") || "0", 10) || 0;
    const oldest = await env.DB.prepare("SELECT MIN(seq) AS s FROM changes").first();
    const oldestSeq = oldest && oldest.s !== null ? oldest.s : null;
    const { results } = await env.DB.prepare("SELECT * FROM changes WHERE seq > ? ORDER BY seq LIMIT 500").bind(since).all();
    const cursor = results.length ? results[results.length - 1].seq : since;
    const truncated = oldestSeq !== null && since < oldestSeq - 1;
    return json({ changes: results, cursor, oldest_seq: oldestSeq, truncated });
  }

  // Files in R2: read any, write own prefix or shared/.
  const fileMatch = path.match(/^\/v1\/files\/(.+)$/);
  if (fileMatch) {
    const key = safeDecode(fileMatch[1]);
    if (key === null) return json({ error: "bad path encoding" }, 400);
    if (method === "GET") {
      const obj = await env.BUCKET.get(key);
      if (!obj) return json({ error: "not found" }, 404);
      // Serve stored bytes as an attachment with a declared type and
      // nosniff: an uploader-chosen text/html must not turn the
      // gateway origin into a content host.
      return new Response(obj.body, {
        headers: {
          "content-type": obj.httpMetadata?.contentType || "application/octet-stream",
          "x-content-type-options": "nosniff",
          "content-disposition": "attachment",
        },
      });
    }
    if (method === "PUT" || method === "DELETE") {
      if (!writableBy(key, botId)) {
        return json({ error: `may only ${method === "PUT" ? "write" : "delete"} bots/<your bot_id>/ or shared/` }, 403);
      }
      if (method === "PUT") {
        await env.BUCKET.put(key, request.body, {
          httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" },
        });
      } else {
        await env.BUCKET.delete(key);
      }
      // R2 and D1 cannot share a transaction; the change-log entry
      // (now WITH the prune the other write paths carry, and an op so
      // peers can tell an upload from a deletion) is best-effort: a
      // logging hiccup never makes a stored file look failed. Peers
      // can always reconcile via the file listing.
      let warning;
      try {
        await env.DB.batch([
          changeStmt(env, botId, "file", key, method === "PUT" ? "put" : "delete"),
          pruneStmt(env),
        ]);
      } catch { warning = "change log entry failed"; }
      return json({ path: key, ok: true, ...(method === "DELETE" ? { deleted: true } : {}), ...(warning ? { warning } : {}) });
    }
    // No HEAD semantics and no ?list=1 shortcut: listing lives at
    // GET /v1/files?prefix= below. (An earlier hint branch here was
    // unreachable for GET and wrong for HEAD, so it was removed.)
    return json({ error: "method not allowed on a file path; use GET /v1/files?prefix= to list" }, 405);
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
}

export default {
  async fetch(request, env) {
    // Every failure — bad encodings, D1 quota errors, binding
    // mistakes — leaves as a JSON error the client can parse and
    // classify, never as a bare non-JSON 500 page.
    try {
      return await handle(request, env);
    } catch (e) {
      return json({ error: "internal", detail: String(e && e.message || e).slice(0, 200) }, 500);
    }
  },
};
