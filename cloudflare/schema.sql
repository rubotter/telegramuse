-- Shared state schema for the Telegram bridge kit (Cloudflare D1).
-- Apply with: wrangler d1 execute bot-shared --remote --file schema.sql
-- or paste into the D1 console.

-- One row per bot: its current role definition (SOUL) and metadata.
CREATE TABLE IF NOT EXISTS bots (
  bot_id       TEXT PRIMARY KEY,          -- e.g. examplebot
  display_name TEXT NOT NULL,
  owner        TEXT,
  definition   TEXT NOT NULL DEFAULT '',  -- SOUL full text (Markdown or JSON)
  version      INTEGER NOT NULL DEFAULT 1,
  updated_at   TEXT NOT NULL              -- ISO 8601 UTC
);

-- Free-form per-bot task status. A bot may only write its own rows.
CREATE TABLE IF NOT EXISTS tasks (
  bot_id     TEXT NOT NULL,
  task_key   TEXT NOT NULL,
  status     TEXT NOT NULL,               -- JSON string
  updated_at TEXT NOT NULL,
  PRIMARY KEY (bot_id, task_key)
);

-- Append-only change log; bots poll with a seq cursor for incremental sync.
CREATE TABLE IF NOT EXISTS changes (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id     TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- definition | task | file
  ref        TEXT,                        -- task_key or file path
  changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_seq ON changes (seq);

-- API tokens, stored ONLY as SHA-256 hex hashes. The Worker maps a
-- presented Bearer token to a bot_id through this table. Revoke by
-- deleting the row. Plaintext tokens are shown once at issuance.
CREATE TABLE IF NOT EXISTS api_tokens (
  token_hash TEXT PRIMARY KEY,
  bot_id     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_bot ON api_tokens (bot_id);
