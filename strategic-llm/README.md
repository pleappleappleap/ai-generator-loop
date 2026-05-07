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

## Message Integration

Will subscribe to `tactical.decisions` (multicast, STOMP) to receive
real-time decision events as they occur. The UI server already subscribes to
this address to push gallery updates; the strategic LLM would be a second
durable subscriber.

```json
{
  "image_uuid":   "string",
  "session_uuid": "string",
  "workflow_id":  "string",
  "decision": {
    "decision":   "accept | retry | inpaint | give_up",
    "reasoning":  "string",
    "confidence": "float 0.0–1.0"
  },
  "verdict": "object (original scorer.result payload)"
}
```
