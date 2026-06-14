-- Decouple embedding storage from model output dimension.
-- Store as text in pgvector format ([f1,f2,...]) and cast to vector at query time.
-- This accepts any dimension without schema changes when the CLIP model changes.
-- The HNSW index is unused (north star similarity is a single-row lookup, not ANN).

ALTER TABLE images              DROP COLUMN IF EXISTS embedding;
ALTER TABLE images              ADD  COLUMN embedding text;

ALTER TABLE north_star          DROP COLUMN IF EXISTS embedding;
ALTER TABLE north_star          ADD  COLUMN embedding text;

ALTER TABLE session_vlm_prompts DROP COLUMN IF EXISTS embedding;
ALTER TABLE session_vlm_prompts ADD  COLUMN embedding text;

DROP INDEX IF EXISTS images_embedding_idx;
