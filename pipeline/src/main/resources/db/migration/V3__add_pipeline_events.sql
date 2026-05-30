-- Mode switch signalling. Written by TacticalLlmCaller on escalate decision.
CREATE TABLE pipeline_events (
    id          BIGSERIAL    PRIMARY KEY,
    type        TEXT         NOT NULL,        -- mode_switch_requested | mode_switch_complete
    reason      TEXT,                         -- escalation context written by tactical LLM
    payload     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    handled_at  TIMESTAMPTZ                   -- null = pending
);
