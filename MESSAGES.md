# Message Schema Contracts

All messages are JSON-encoded UTF-8. The pipeline uses ActiveMQ Artemis with
AMQP 1.0 (all queues are consumed by the Spring Boot pipeline).

Address types: **anycast** = queue semantics (one consumer receives).
All addresses below use anycast routing.

---

## Anycast Addresses

### `loop.generate`
**Publishers:** `WorkflowController` (workflow start-run), `ChatWsHandler` (chat session)
**Consumer:** `ComfyUiWorker`
**Protocol:** AMQP 1.0

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

### `loop.retry`
**Publisher:** `TacticalLlmCaller` (retry decision)
**Consumer:** `ComfyUiWorker`
**Protocol:** AMQP 1.0

Same schema as `loop.generate`. Carries revised prompt and/or workflow params from the
tactical LLM.

### `loop.inpaint`
**Publisher:** `TacticalLlmCaller` (inpaint decision)
**Consumer:** `ComfyUiWorker`
**Protocol:** AMQP 1.0

Same schema as `loop.generate`. The workflow params carry inpainting region and prompt
information derived from the tactical LLM decision.

### `loop.generated`
**Publisher:** `ComfyUiWorker` (after ComfyUI generation completes)
**Consumer:** `Scorer`
**Protocol:** AMQP 1.0

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

### `loop.verdicts`
**Publisher:** `Scorer` (candidates only — images that pass all thresholds)
**Consumer:** `TacticalLlmCaller`
**Protocol:** AMQP 1.0

```json
{
  "image_uuid":      "string",
  "session_uuid":    "string",
  "workflow_id":     "string",
  "conversation_id": "string",
  "sequence_number": "integer",
  "prompt":          "string",
  "workflow_path":   "string",
  "image_path":      "string",
  "scores": {
    "clip":     {"image_uuid": "string", "clip_score": "float", "image_embedding": "array[float] 512-dim"},
    "artifact": {"image_uuid": "string", "ai_confidence": "float"},
    "vlm":      {
      "image_uuid":               "string",
      "photorealism":             "float 0-10",
      "anatomical_coherence":     "float 0-10",
      "interaction_plausibility": "float 0-10",
      "lighting_consistency":     "float 0-10",
      "prompt_adherence":         "float 0-10",
      "issues":                   ["string"],
      "recommendations":          ["string"]
    }
  },
  "north_star_similarity": "float 0.0-1.0 (null if no active north star)"
}
```

Images rejected by threshold logic are written to PostgreSQL with `status = 'rejected'`
and a `reason` field but are not published to any queue.

### `pipeline.dead`
**Publisher:** Artemis DLX (from all failed queues)
**Consumer:** none (inspect via Artemis console at `http://localhost:12009`)
**Protocol:** AMQP 1.0

Original message body preserved. Artemis adds headers:
- `_AMQ_ORIG_ADDRESS`  -  source address
- `_AMQ_ORIG_ROUTING_TYPE`  -  anycast or multicast
- `_AMQ_ACTUAL_EXPIRY`  -  when the message expired (if applicable)

---

## Scorer HTTP API

The three scorer sidecars are stateless HTTP services. They are called directly
by the `Scorer` Java class (not via the broker).

### CLIP scorer — `http://localhost:12002`

```
POST /score
{ "image_path": "string" }
→ { "image_uuid": "string", "clip_score": "float 0.0-1.0", "image_embedding": "array[float] 512-dim" }

POST /embed_text
{ "text": "string" }
→ { "embedding": "array[float] 512-dim" }
```

### Artifact scorer — `http://localhost:12003`

```
POST /score
{ "image_path": "string" }
→ { "image_uuid": "string", "ai_confidence": "float 0.0-1.0" }
```

### VLM scorer — `http://localhost:12004`

```
POST /score
{ "image_path": "string", "prompt": "string" }
→ { "image_uuid": "string", "photorealism": float, "anatomical_coherence": float,
    "interaction_plausibility": float, "lighting_consistency": float,
    "prompt_adherence": float, "issues": [...], "recommendations": [...] }

POST /analyze
{ "image_url": "string", "question": "string" }
→ { "answer": "string" }
```

The `/analyze` endpoint is called by `TacticalLlmCaller` during tool-use turns of the
agentic decision loop.

---

## Pipeline REST and WebSocket API

The Spring Boot pipeline exposes the following endpoints on port 12000:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the browser UI (`static/index.html`) |
| `POST` | `/session/start` | Create a new conversation (writes to PostgreSQL) |
| `POST` | `/workflow/new` | Create a new workflow and start a run |
| `POST` | `/workflow/{id}/resume` | Resume an existing workflow with new budget |
| `POST` | `/workflow/{id}/start-run` | Publish a `loop.generate` message to start generation |
| `POST` | `/feedback` | Submit a thumbs rating |
| `GET` | `/history` | Fetch conversation context (chat + feedback) |
| `GET` | `/image/{uuid}` | Serve a generated image (local file) |
| `WS` | `/ws/gallery` | WebSocket — pushes `image_ready` and `decision` events to browsers |
| `GET` | `/strategic/` | Redirects to strategic UI (`/strategic/index.html`) |
| `POST` | `/strategic/run` | Trigger a strategic session manually |
| `GET` | `/strategic/status` | Current strategic session state |
| `POST` | `/north-star` | Write a new north star directly (bypasses strategic LLM) |

### WebSocket gallery events

The `/ws/gallery` WebSocket pushes two event types to connected browsers:

**`image_ready`** — emitted by `ComfyUiWorker` after generation:
```json
{ "type": "image_ready", "image_uuid": "string", "image_path": "string",
  "session_uuid": "string", "workflow_id": "string" }
```

**`decision`** — emitted by `TacticalLlmCaller` after each decision:
```json
{
  "type": "decision",
  "image_uuid": "string",
  "session_uuid": "string",
  "workflow_id": "string",
  "decision": {
    "decision":    "accept | retry | inpaint | give_up | escalate",
    "reasoning":   "string",
    "confidence":  "float 0.0-1.0",
    "retry_prompt":      "string (if retry)",
    "retry_params":      "object (if retry)",
    "inpaint_regions":   "array (if inpaint)",
    "inpaint_prompt":    "string (if inpaint)"
  },
  "scores": "object (full scorer results from loop.verdicts)"
}
```
