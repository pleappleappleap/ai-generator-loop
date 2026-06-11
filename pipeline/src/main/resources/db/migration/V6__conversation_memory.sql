CREATE TABLE conversation_memory (
    id              BIGSERIAL   PRIMARY KEY,
    conversation_id UUID        NOT NULL REFERENCES conversations UNIQUE,
    content         TEXT        NOT NULL,
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
