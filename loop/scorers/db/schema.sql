-- pipeline.db schema
-- Applied on startup by db::init_schema() then db::migrate().

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = FULL;

-- ── Conversation / workflow hierarchy ─────────────────────────────────────────

-- Top-level creative session. Owns the chat thread.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL DEFAULT 'Untitled',
    created_at      INTEGER NOT NULL
);

-- A stable creative direction within a conversation.
-- Persists across multiple generation runs. Can be paused and resumed.
-- budget_policy: 'fresh' (reset on resume) | 'accumulated' (continue counters).
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id     TEXT    PRIMARY KEY,
    conversation_id TEXT    NOT NULL,
    name            TEXT    NOT NULL DEFAULT 'Untitled Workflow',
    workflow_path   TEXT    NOT NULL,
    description     TEXT,
    budget_policy   TEXT    NOT NULL DEFAULT 'fresh',
    max_retries     INTEGER NOT NULL DEFAULT 3,
    max_inpaints    INTEGER NOT NULL DEFAULT 2,
    created_at      INTEGER NOT NULL
);

-- User thumbs up/down ratings on individual images.
CREATE TABLE IF NOT EXISTS user_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    image_uuid  TEXT    NOT NULL,
    workflow_id TEXT    NOT NULL,
    run_id      TEXT    NOT NULL,
    rating      TEXT    NOT NULL,   -- 'up' | 'down'
    comment     TEXT,
    created_at  INTEGER NOT NULL
);

-- Persistent chat thread between user and tactical LLM.
-- workflow_id is nullable — messages may be workflow-specific or general.
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT    NOT NULL,
    workflow_id     TEXT,
    role            TEXT    NOT NULL,   -- 'user' | 'assistant'
    content         TEXT    NOT NULL,
    created_at      INTEGER NOT NULL
);

-- XA coordinator transaction log.
-- Written with fsync before phase 2. Scanned on startup for crash recovery.
CREATE TABLE IF NOT EXISTS xa_log (
    xid           TEXT    PRIMARY KEY,
    state         TEXT    NOT NULL,   -- prepared | committed | rolledback
    participants  TEXT    NOT NULL,   -- JSON array of resource names
    created_at    INTEGER NOT NULL,
    resolved_at   INTEGER
);

-- In-flight scorer session accumulation.
-- Replaces agg:session:{uuid} and ldb:session:{uuid} Redis keys.
-- Written by comfyui_worker (via coordinator API) on generation start.
-- Updated by aggregator as scorer results arrive.
-- Read by lancedb_manager on terminal event; row deleted after LanceDB write.
CREATE TABLE IF NOT EXISTS scorer_session (
    image_uuid       TEXT    PRIMARY KEY,
    session_uuid     TEXT    NOT NULL,
    sequence_number  INTEGER NOT NULL,
    prompt           TEXT,
    workflow_path    TEXT,
    workflow_params  TEXT,    -- JSON
    workflow_id      TEXT,    -- references workflows.workflow_id
    conversation_id  TEXT,    -- references conversations.conversation_id
    clip             TEXT,    -- JSON, null until CLIP result arrives
    artifact         TEXT,    -- JSON, null until artifact result arrives
    vlm              TEXT,    -- JSON, null until VLM result arrives
    verdict          TEXT,    -- null until aggregator decides
    rejection_reason TEXT,
    image_path       TEXT,
    created_at       INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL
);

-- Tactical LLM per-session budget.
-- Replaces tactical:budget:{uuid} Redis keys.
-- Written by session.py (via coordinator API) on session creation.
-- Updated by tactical_llm.py (via coordinator API) on each decision.
CREATE TABLE IF NOT EXISTS tactical_budget (
    session_uuid   TEXT    PRIMARY KEY,
    workflow_id    TEXT,    -- references workflows.workflow_id
    conversation_id TEXT,   -- references conversations.conversation_id
    retries_used   INTEGER NOT NULL DEFAULT 0,
    inpaints_used  INTEGER NOT NULL DEFAULT 0,
    max_retries    INTEGER NOT NULL,
    max_inpaints   INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL
);
