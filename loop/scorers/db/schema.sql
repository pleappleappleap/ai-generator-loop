-- pipeline.db schema
-- Applied on startup by db::init_schema().

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = FULL;

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
    retries_used   INTEGER NOT NULL DEFAULT 0,
    inpaints_used  INTEGER NOT NULL DEFAULT 0,
    max_retries    INTEGER NOT NULL,
    max_inpaints   INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL
);
