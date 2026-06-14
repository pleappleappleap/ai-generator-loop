-- CLIP model is ViT-L/14 which produces 768-dim embeddings, not 512.
-- Resize all embedding columns and rebuild the HNSW index.

ALTER TABLE images      DROP COLUMN IF EXISTS embedding;
ALTER TABLE images      ADD  COLUMN embedding vector(768);

ALTER TABLE north_star  DROP COLUMN IF EXISTS embedding;
ALTER TABLE north_star  ADD  COLUMN embedding vector(768);

ALTER TABLE session_vlm_prompts DROP COLUMN IF EXISTS embedding;
ALTER TABLE session_vlm_prompts ADD  COLUMN embedding vector(768);

DROP INDEX IF EXISTS images_embedding_idx;
CREATE INDEX images_embedding_idx ON images USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
