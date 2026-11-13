"""DDL and idempotent schema init. Single source of truth for table shapes."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    root_id         TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    agent_id        TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'shared',
    kind            TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',
    title           TEXT,
    content         TEXT NOT NULL,
    search_text     TEXT NOT NULL DEFAULT '',
    importance      REAL NOT NULL DEFAULT 0.5,
    source          TEXT NOT NULL DEFAULT 'manual',
    status          TEXT NOT NULL DEFAULT 'active',
    dedup_hash      TEXT,
    superseded_by   TEXT,
    conflict_of     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_recalled_at TEXT,
    recalled_times  INTEGER NOT NULL DEFAULT 0,
    valid_from      TEXT,
    valid_until     TEXT,
    embedding       BLOB,
    topic           TEXT,            -- LLM 定稿 topic（每条唯一；生成时为空）
    pending_since   TEXT,
    cluster_id      TEXT,            -- 聚类 id（生成时为空，预聚类/直接聚类时生成）
    cluster_stats   TEXT,            -- unclustered / pre_clustered / clustered
    episode_id      TEXT,            -- FK -> episodes.id (context anchor)
    normalized_text TEXT             -- write-time relative-date-normalized content (raw content kept)
);
CREATE INDEX IF NOT EXISTS idx_memories_topic  ON memories(topic);
CREATE INDEX IF NOT EXISTS idx_memories_cluster ON memories(cluster_id);
CREATE INDEX IF NOT EXISTS idx_memories_pending ON memories(pending_since);
CREATE INDEX IF NOT EXISTS idx_memories_root   ON memories(root_id);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_kind   ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_dedup  ON memories(dedup_hash);
CREATE INDEX IF NOT EXISTS idx_memories_scope  ON memories(scope, agent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    search_text,
    tokenize='unicode61'
);

-- Episode layer: ONE row per session (session_id UNIQUE). Each compaction overwrites
-- the stored `summary` (Hermes cumulative summary) and bumps `revision`. Mirrors the
-- host transcript, so it is allowed to be physically deleted on TTL.
CREATE TABLE IF NOT EXISTS episodes (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'shared',
    session_id  TEXT NOT NULL UNIQUE,   -- one episode per session
    mode        TEXT,                   -- work / chat
    trigger     TEXT,                   -- compaction / manual
    summary     TEXT NOT NULL,          -- latest Hermes cumulative summary ("" for chat windows)
    summary_kind TEXT NOT NULL DEFAULT 'compaction',  -- compaction | window
    message_id_range TEXT,              -- JSON [start_id, end_id] of the processed slice
    revision    INTEGER NOT NULL DEFAULT 1,  -- bumped on each compaction / batch
    status      TEXT NOT NULL DEFAULT 'active',  -- active / archived / expired
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    valid_until TEXT                    -- TTL; expired -> physically DELETEd
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_valid   ON episodes(valid_until);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Per-conversation processor state (chat batch cursor, activity clock, pending batch).
-- One row per host session; a generic KV per session so future tasks can add state
-- without new columns. Cursor-based => no gap, no double-process.
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    cursor_id     INTEGER NOT NULL DEFAULT 0,  -- last processed state.db message rowid
    last_activity TEXT,                        -- ISO; idle detection
    pending_batch INTEGER NOT NULL DEFAULT 0,  -- 1 when a chat batch is due/pending
    batch_start   INTEGER NOT NULL DEFAULT 0,  -- window start snapshot (for logging/debug)
    fails         INTEGER NOT NULL DEFAULT 0,  -- consecutive extraction failures
    updated_at    TEXT NOT NULL
);

-- Topic clustering task QUEUE. One task per candidate cluster (cluster_id UNIQUE =>
-- no duplicate tasks; accumulates until the idle runner finalizes them). status:
--   queued  = waiting for the idle LLM finalize
--   done    = finalized (topic set, cluster_stats=clustered)
--   dropped = LLM said not-same-matter (members reset to unclustered)
-- A cluster whose size later grows (new same_matter member joins) is re-queued by
-- enqueue_topic_tasks; cached_members detects that growth => no gap.
CREATE TABLE IF NOT EXISTS topic_tasks (
    cluster_id     TEXT PRIMARY KEY,
    size           INTEGER NOT NULL DEFAULT 0,  -- member count when enqueued
    cached_members TEXT,                        -- JSON [{id,content}] snapshot at enqueue
    status         TEXT NOT NULL DEFAULT 'queued',  -- queued | done | dropped
    fails          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_tasks_status ON topic_tasks(status);
"""


def _add_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """Idempotent ADD COLUMN that is safe when several processes/threads run it
    concurrently: re-check under the write lock, tolerate 'duplicate column'."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(_DDL)
    conn.commit()
    # Additive, idempotent columns (safe on an existing DB that holds live data, and a
    # no-op on a fresh DB because CREATE above already includes them).
    _add_column(conn, "memories", "normalized_text", "TEXT")
    # -- NO backward-compat migrations: this is an early-stage design; DBs created
    #    before the current schema are simply recreated (the CREATE above is authoritative).
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
