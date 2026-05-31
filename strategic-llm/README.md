# Strategic LLM

Serves the strategic review model via `mlx_lm.server` (OpenAI-compatible API on port 12005).
Managed by `strategic_llm.sh` and orchestrated by the root `strategic.sh` super-component script.

## Role

The strategic director reviews accumulated generation history and provides high-level creative
direction. It runs in a dedicated session that is mutually exclusive with the Loop super-component  - 
only one may be running at a time. Middleware (Artemis + PostgreSQL) stays up across mode switches.

A strategic session is triggered in two ways:

1. **Operator-initiated**: `POST /strategic/run` from the browser UI at
   `http://localhost:12000/strategic/`.
2. **LLM-initiated**: the tactical LLM emits an `escalate` decision, which causes the pipeline to
   write a `pipeline_events` row and shut down gracefully. `strategic.sh` detects the pending
   escalation and starts the strategic session automatically.

During a session the strategic LLM reads: current `north_star`, `session_direction`,
recent images with scores, user feedback, and `taste_synthesis`. It updates the `north_star`
and optionally the `session_direction` for the next Loop session.

## Files

| File | Role |
|------|------|
| `strategic_llm.sh` | Component manager  -  start/stop/health for `mlx_lm.server` |
| `system_prompt.md` | System prompt for the strategic director role |

The strategic UI is a static page served by the Spring Boot pipeline at `/strategic/index.html`.
There is no separate UI server process.

## Model

**Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated** (bf16 safetensors, ~160 GB on disk)

- Architecture: 80B total parameters, ~3B active per forward pass (ultra-sparse MoE)
- `mlx_lm.server` loads bf16 safetensors directly; MLX demand-pages inactive expert arrays
  from SSD  -  working memory footprint ~40-50 GB
- Thinking variant: extended chain-of-thought; no token budget cap for strategic sessions
- Download: `hf download huihui-ai/Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated`

## Management

```bash
# Full strategic super-component (LLM + pipeline)
strategic.sh start
strategic.sh stop
strategic.sh status
strategic.sh health

# LLM server only
strategic-llm/strategic_llm.sh start
strategic-llm/strategic_llm.sh health    # GET /v1/models
```

## Health Check

`mlx_lm.server` has no `/health` endpoint. Use:

```bash
curl http://localhost:12005/v1/models
```

## Output Format

The strategic LLM responds with a JSON object:

```json
{
  "analysis":         "Summary of what the session produced and why direction needs updating.",
  "north_star":       "Updated north star statement (replaces current).",
  "session_direction": "Optional: specific direction for the next session.",
  "reasoning":        "One sentence  -  why this direction change."
}
```

`<think>` blocks and Markdown code fences are stripped by `StrategicLlmCaller` before parsing.
