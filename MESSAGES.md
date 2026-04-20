# Message Schema Contracts

All messages are JSON-encoded UTF-8.

---

## Queues

### `loop.request`
**Publisher:** Tactical LLM  **Consumer:** `comfyui_worker.py`

```json
{
  "image_uuid": "string",
  "session_uuid": "string",
  "sequence_number": "integer",
  "workflow_path": "string",
  "prompt": "string",
  "workflow_params": {
    "checkpoint": "string",
    "steps": "integer",
    "cfg": "float",
    "sampler": "string",
    "seed": "integer (optional)"
  }
}
```

### `scorer.result`
**Publisher:** `aggregator` (Rust)  **Consumer:** Tactical LLM

```json
{
  "image_uuid": "string",
  "verdict": "candidate | rejected",
  "reason": "clip_threshold | artifact_threshold (omitted for candidate)",
  "scores": {
    "clip": {
      "image_uuid": "string",
      "clip_score": "float 0.0–1.0",
      "image_embedding": "array[float] 512-dim"
    },
    "artifact": {
      "image_uuid": "string",
      "ai_confidence": "float 0.0–1.0"
    },
    "vlm": {
      "image_uuid": "string",
      "photorealism": "float 0–10",
      "anatomical_coherence": "float 0–10",
      "interaction_plausibility": "float 0–10",
      "lighting_consistency": "float 0–10",
      "prompt_adherence": "float 0–10",
      "issues": ["string"],
      "recommendations": ["string"]
    }
  }
}
```

---

## Topic Exchanges

### `loop.events` — routing key: `loop.complete.<image_uuid>`
**Publisher:** `comfyui_worker.py`  **Consumer:** router via `router.loop`

```json
{
  "image_uuid": "string",
  "session_uuid": "string",
  "sequence_number": "integer",
  "prompt_id": "string",
  "image_path": "string",
  "prompt": "string",
  "workflow_path": "string",
  "workflow_params": "object"
}
```

### `scorer.requests` — routing key: `score.<image_uuid>`
**Publisher:** router  **Consumers:** all scorers
Payload: unchanged `loop.complete.*` message.

### `scorer.events` — routing key: `cancel.<image_uuid>`
**Publisher:** aggregator  **Consumers:** all scorers, lancedb_manager

```json
{
  "image_uuid": "string",
  "reason": "clip_threshold | artifact_threshold"
}
```

### `scorer.results` — routing keys: `clip.*`, `artifact.*`, `vlm.*`

**clip.<image_uuid>** — Publisher: `clip_scorer.py`
```json
{"image_uuid": "string", "clip_score": "float", "image_embedding": "array[float] 512-dim"}
```

**artifact.<image_uuid>** — Publisher: `artifact_scorer.py`
```json
{"image_uuid": "string", "ai_confidence": "float"}
```

**vlm.<image_uuid>** — Publisher: `vlm_scorer.py`
```json
{
  "image_uuid": "string",
  "photorealism": "float 0–10",
  "anatomical_coherence": "float 0–10",
  "interaction_plausibility": "float 0–10",
  "lighting_consistency": "float 0–10",
  "prompt_adherence": "float 0–10",
  "issues": ["string"],
  "recommendations": ["string"]
}
```

### `loop.accepted` — routing key: `accepted.<image_uuid>`
**Publisher:** Tactical LLM  **Consumer:** lancedb_manager

```json
{
  "image_uuid": "string",
  "image_path": "string"
}
```

---

## Redis Key Namespaces

| Prefix | Owner | TTL | Purpose |
|--------|-------|-----|---------|
| `agg:session:<uuid>` | aggregator | 60s | Aggregator correlation state |
| `ldb:session:<uuid>` | lancedb_manager | 120s | LanceDB manager correlation state |
