# Message Schema Contracts

All messages are JSON-encoded UTF-8. The pipeline uses ActiveMQ Artemis with
two protocols: AMQP 1.0 (Rust components) and STOMP (Python components).

Address types: **anycast** = queue semantics (one consumer receives);
**multicast** = topic semantics (all subscribers receive).

---

## Anycast Addresses (queue semantics)

### `loop.request`
**Publisher:** UI server (`server.py`), tactical LLM (retry)
**Consumer:** `comfyui_worker.py`
**Protocol:** STOMP

```json
{
  "image_uuid":      "string",
  "session_uuid":    "string",
  "workflow_id":     "string",
  "conversation_id": "string",
  "sequence_number": "integer",
  "workflow_path":   "string",
  "prompt":          "string",
  "workflow_params": {
    "checkpoint": "string",
    "steps":      "integer",
    "cfg":        "float",
    "sampler":    "string",
    "seed":       "integer (optional)"
  }
}
```

### `aggregator.clip.queue`
**Publisher:** `clip_scorer.py`  **Consumer:** `aggregator` (Rust)
**Protocol:** STOMP → AMQP 1.0

```json
{"image_uuid": "string", "clip_score": "float 0.0–1.0", "image_embedding": "array[float] 512-dim"}
```

### `aggregator.artifact.queue`
**Publisher:** `artifact_scorer.py`  **Consumer:** `aggregator` (Rust)
**Protocol:** STOMP → AMQP 1.0

```json
{"image_uuid": "string", "ai_confidence": "float 0.0–1.0"}
```

### `aggregator.vlm.queue`
**Publisher:** `vlm_scorer.py`  **Consumer:** `aggregator` (Rust)
**Protocol:** STOMP → AMQP 1.0

```json
{
  "image_uuid":               "string",
  "photorealism":             "float 0–10",
  "anatomical_coherence":     "float 0–10",
  "interaction_plausibility": "float 0–10",
  "lighting_consistency":     "float 0–10",
  "prompt_adherence":         "float 0–10",
  "issues":          ["string"],
  "recommendations": ["string"]
}
```

### `scorer.result`
**Publisher:** `aggregator` (Rust)  **Consumer:** `tactical_llm.py`
**Protocol:** AMQP 1.0 → STOMP

```json
{
  "image_uuid":      "string",
  "verdict":         "candidate | rejected",
  "reason":          "clip_threshold | artifact_threshold (omitted for candidate)",
  "prompt":          "string",
  "session_uuid":    "string",
  "workflow_id":     "string",
  "conversation_id": "string",
  "workflow_path":   "string",
  "sequence_number": "integer",
  "scores": {
    "clip":     {"image_uuid": "string", "clip_score": "float", "image_embedding": "array[float]"},
    "artifact": {"image_uuid": "string", "ai_confidence": "float"},
    "vlm":      {"image_uuid": "string", "photorealism": "float", "anatomical_coherence": "float",
                 "interaction_plausibility": "float", "lighting_consistency": "float",
                 "prompt_adherence": "float", "issues": ["string"], "recommendations": ["string"]}
  }
}
```

### `pipeline.dead`
**Publisher:** Artemis DLX (from all failed queues)  **Consumer:** `monitor.py`
**Protocol:** AMQP 1.0

Original message body preserved. Artemis adds headers:
- `_AMQ_ORIG_ADDRESS` — source address
- `_AMQ_ORIG_ROUTING_TYPE` — anycast or multicast
- `_AMQ_ACTUAL_EXPIRY` — when the message expired (if applicable)

---

## Multicast Addresses (topic semantics)

### `loop.events`
**Publisher:** `comfyui_worker.py`
**Consumers:** `router` (Rust), UI server (`server.py`)
**Protocol:** STOMP → AMQP 1.0

```json
{
  "image_uuid":      "string",
  "session_uuid":    "string",
  "workflow_id":     "string",
  "conversation_id": "string",
  "sequence_number": "integer",
  "prompt_id":       "string",
  "image_path":      "string",
  "prompt":          "string",
  "workflow_path":   "string",
  "workflow_params": "object"
}
```

The UI server subscribes to `loop.events` via STOMP to cache the
`image_uuid → image_path` mapping so it can serve generated images to the
browser via `/image/{uuid}`.

### `scorer.requests`
**Publisher:** `router` (Rust)  **Consumers:** all scorers (durable STOMP subscriptions)
**Protocol:** AMQP 1.0 → STOMP
**Message property:** `subject = score.<image_uuid>`

Payload: unchanged `loop.events` message body.

### `scorer.events`
**Publisher:** `aggregator` (Rust)
**Consumers:** all scorers (cancel flag), `lancedb_manager` (Rust)
**Protocol:** AMQP 1.0
**Message property:** `subject = cancel.<image_uuid>`

```json
{"image_uuid": "string"}
```

### `loop.accepted`
**Publisher:** `tactical_llm.py`  **Consumer:** `lancedb_manager` (Rust)
**Protocol:** STOMP → AMQP 1.0

```json
{
  "image_uuid": "string",
  "image_path": "string",
  "scores":     "object (full scorer results)"
}
```

### `tactical.decisions`
**Publisher:** `tactical_llm.py`
**Consumers:** UI server (`server.py`), strategic LLM (planned)
**Protocol:** STOMP

The UI server subscribes to `tactical.decisions` to push live decision updates
to connected browser clients via the WebSocket gallery endpoint.

```json
{
  "image_uuid":   "string",
  "session_uuid": "string",
  "workflow_id":  "string",
  "decision": {
    "decision":    "accept | retry | inpaint | give_up",
    "reasoning":   "string",
    "confidence":  "float 0.0–1.0",
    "retry_prompt":  "string (if retry)",
    "retry_params":  "object (if retry)",
    "inpaint_regions": "array (if inpaint)",
    "inpaint_prompt":  "string (if inpaint)"
  },
  "verdict": "object (original scorer.result payload)"
}
```

---

## Coordinator Unix Socket API

Socket path: `{database.path}.sock` (e.g. `pipeline.db.sock`).
Protocol: newline-delimited JSON (request → response per connection).

### Conversation and Workflow Operations

```json
{"op":"ConversationCreate", "name": "My Project"}
{"op":"WorkflowCreate",     "conversation_id":"...", "workflow_path":"sdxl.json",
                             "max_retries":3, "max_inpaints":2}
{"op":"WorkflowResume",     "workflow_id":"...", "budget_policy":"carry",
                             "max_retries":3, "max_inpaints":2}
{"op":"FeedbackAdd",        "image_uuid":"...", "workflow_id":"...", "run_id":"...",
                             "rating":"up | down", "comment":"string (optional)"}
{"op":"ChatAdd",            "conversation_id":"...", "role":"user | assistant",
                             "content":"string"}
{"op":"ContextGet",         "conversation_id":"...", "workflow_id":"...", "limit":20}
```

### Session Budget Operations

```json
{"op":"BudgetInit",   "session_uuid":"...", "max_retries":3, "max_inpaints":2}
{"op":"BudgetGet",    "session_uuid":"..."}
{"op":"BudgetUpdate", "session_uuid":"...", "field":"retries_used | inpaints_used"}
{"op":"SessionInit",  "image_uuid":"...", "session_uuid":"...", "sequence_number":1,
                      "prompt":"...", "workflow_path":"...", "workflow_params":"{}",
                      "score_timeout_secs":60}
```

### Responses

```json
{"ok": true}
{"ok": true, "conversation_id": "uuid"}
{"ok": true, "workflow_id": "uuid", "conversation_id": "uuid", "workflow_path": "sdxl.json"}
{"ok": true, "run_id": "uuid"}
{"ok": true, "retries_used":1, "inpaints_used":0, "max_retries":3, "max_inpaints":2}
{"ok": true, "chat": [...], "feedback": [...]}
{"ok": false, "error": "session not found"}
```

---

## UI Server REST and WebSocket API

The UI server (`loop/ui/server.py`) exposes the following endpoints on the
configured port (default 7860):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the browser UI (`static/index.html`) |
| `POST` | `/session/start` | Create a new conversation via coordinator `ConversationCreate` |
| `POST` | `/workflow/new` | Create + resume a workflow (`WorkflowCreate` then `WorkflowResume`) |
| `POST` | `/workflow/{id}/resume` | Resume an existing workflow (`WorkflowResume`) |
| `POST` | `/workflow/{id}/start-run` | Publish a `loop.request` message to start image generation |
| `POST` | `/feedback` | Submit a thumbs rating (`FeedbackAdd`) |
| `GET` | `/history` | Fetch conversation context (`ContextGet`) |
| `GET` | `/image/{uuid}` | Serve a generated image (local file or proxied HTTP URL) |
| `WS` | `/ws/gallery` | WebSocket — pushes `image_ready` and `decision` events to browsers |
