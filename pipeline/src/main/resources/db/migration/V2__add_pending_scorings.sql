-- V2: durable staging table for Scorer
-- Scorer holds this while the three ML sidecar calls are in flight (seconds to ~30 s).
-- No intermediate state column needed: all three calls are idempotent, so on crash
-- recovery the poller simply reruns all three from the stored payload.
CREATE TABLE pending_scorings (
    image_uuid   UUID        PRIMARY KEY,
    session_uuid UUID        NOT NULL,
    payload      JSONB       NOT NULL,  -- full loop.generated message; stored for recovery
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
