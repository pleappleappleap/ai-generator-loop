-- V1: initial pipeline schema
-- PostgreSQL 17 + pgvector; applied by Flyway on first startup.

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Core tables ───────────────────────────────────────────────────────────────

CREATE TABLE conversations (
    conversation_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL DEFAULT 'Untitled',
    status          TEXT        NOT NULL DEFAULT 'active',  -- active | cancelled
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflows (
    workflow_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations,
    name            TEXT        NOT NULL DEFAULT 'Untitled Workflow',
    workflow_path   TEXT        NOT NULL,
    description     TEXT,
    budget_policy   TEXT        NOT NULL DEFAULT 'fresh',  -- fresh | accumulated
    max_retries     INT         NOT NULL DEFAULT 3,
    max_inpaints    INT         NOT NULL DEFAULT 2,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE images (
    image_uuid       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_uuid     UUID        NOT NULL,
    sequence_number  INT         NOT NULL,
    prompt           TEXT,
    workflow_path    TEXT,
    workflow_params  JSONB,
    workflow_id      UUID        REFERENCES workflows,
    conversation_id  UUID        REFERENCES conversations,
    clip_score       FLOAT,
    artifact_score   FLOAT,
    vlm_scores       JSONB,
    vlm_issues       TEXT[],
    vlm_recs         TEXT[],
    verdict          TEXT,         -- candidate | rejected
    rejection_reason TEXT,
    decision         TEXT,         -- accepted | retry | inpaint | give_up
    image_path       TEXT,
    embedding        vector(512),  -- CLIP image embedding
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON images USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE budget (
    session_uuid    UUID        PRIMARY KEY,
    workflow_id     UUID        REFERENCES workflows,
    conversation_id UUID        REFERENCES conversations,
    retries_used    INT         NOT NULL DEFAULT 0,
    inpaints_used   INT         NOT NULL DEFAULT 0,
    max_retries     INT         NOT NULL,
    max_inpaints    INT         NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE chat_messages (
    id              BIGSERIAL   PRIMARY KEY,
    conversation_id UUID        NOT NULL REFERENCES conversations,
    workflow_id     UUID        REFERENCES workflows,
    role            TEXT        NOT NULL,  -- user | assistant
    content         TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_feedback (
    id              BIGSERIAL   PRIMARY KEY,
    image_uuid      UUID        NOT NULL REFERENCES images,
    workflow_id     UUID        REFERENCES workflows,
    session_uuid    UUID        NOT NULL,
    rating          TEXT        NOT NULL,  -- up | down
    comment         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Durable staging tables (XA 2PC) ──────────────────────────────────────────
-- Each table holds at most one row. The worker is single-item: it completes or
-- recovers the current row before consuming the next broker message.

-- ComfyUI worker holds this while image generation is in flight (20-120 s).
-- prompt_id is null until ComfyUI accepts the job; used for crash recovery.
CREATE TABLE pending_generations (
    image_uuid   UUID        PRIMARY KEY,
    session_uuid UUID        NOT NULL,
    prompt       TEXT,
    type         TEXT        NOT NULL DEFAULT 'generate',  -- generate | retry | inpaint
    prompt_id    TEXT,       -- null → ComfyUI not yet reached; non-null → recheck history
    payload      JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tactical LLM caller holds this while LLM inference is in flight (10-30 s).
-- decision is null until the LLM responds; used for crash recovery.
CREATE TABLE pending_decisions (
    image_uuid      UUID        PRIMARY KEY,
    session_uuid    UUID        NOT NULL,
    verdict_payload JSONB       NOT NULL,  -- full loop.verdicts message; stored for recovery
    decision        TEXT,       -- null → LLM not yet reached; accepted | retry | inpaint | give_up
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Creative context tables ───────────────────────────────────────────────────

-- Long-term artistic vision. One active row (superseded_at IS NULL).
-- Updated by user or strategic LLM; old row gets superseded_at = now().
CREATE TABLE north_star (
    id            BIGSERIAL   PRIMARY KEY,
    content       TEXT        NOT NULL,
    embedding     vector(512),
    created_by    TEXT        NOT NULL DEFAULT 'user',  -- user | strategic_llm
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at TIMESTAMPTZ
);

-- Per-session creative brief. One active row per workflow (superseded_at IS NULL).
CREATE TABLE session_direction (
    id              BIGSERIAL   PRIMARY KEY,
    conversation_id UUID        REFERENCES conversations,
    workflow_id     UUID        REFERENCES workflows,
    content         TEXT        NOT NULL,
    embedding       vector(512),
    created_by      TEXT        NOT NULL DEFAULT 'user',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at   TIMESTAMPTZ
);

-- Synthesised taste profile. Written by tactical LLM when feedback threshold met.
CREATE TABLE taste_synthesis (
    id                   BIGSERIAL   PRIMARY KEY,
    conversation_id      UUID        REFERENCES conversations,
    workflow_id          UUID        REFERENCES workflows,
    content              TEXT        NOT NULL,
    source_feedback_count INT        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
