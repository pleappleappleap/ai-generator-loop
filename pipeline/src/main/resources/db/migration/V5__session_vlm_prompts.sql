CREATE TABLE session_vlm_prompts (
    id              BIGSERIAL   PRIMARY KEY,
    conversation_id UUID        NOT NULL REFERENCES conversations,
    eval_prompt     TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX session_vlm_prompts_conv_idx ON session_vlm_prompts (conversation_id, created_at DESC);
