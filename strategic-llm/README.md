# Strategic LLM

Planned subsystem. Not yet implemented.

## Planned Role

Analyzes accumulated generation history in LanceDB across sessions
to improve generation quality over time. Operates on LanceDB directly
— no real-time queue subscriptions.

## Interface

Reads from LanceDB via `~/ai-image/lancedb_schema.py` and
`~/ai-image/lancedb_manager.py`.

Key query patterns:
- Vector search on `prompt_embedding` for similar past sessions
- Filter on `session_uuid` + order by `sequence_number` for trend analysis
- Vector search on `image_embedding` for visual similarity
