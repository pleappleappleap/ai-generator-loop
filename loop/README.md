# Loop Subsystem

The generation loop subsystem. Accepts generation requests via the `loop.generate`
anycast queue, generates images via ComfyUI, scores them across three independent
dimensions, and feeds candidates to the tactical LLM for a decision.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `pipeline` | Java / Spring Boot | Browser UI backend; drives ComfyUI; calls scorer sidecars; runs tactical LLM agentic loop |
| `clip_scorer.py` | Python (FastAPI) | ViT-L-14 CLIP semantic similarity; exposes `/score` and `/embed_text` |
| `artifact_scorer.py` | Python (FastAPI) | AI artifact detection; exposes `/score` |
| `vlm_scorer.py` | Python (FastAPI) | VLM holistic evaluation + `/analyze` endpoint for tactical LLM tool use |

## Starting the Loop

```bash
~/soxhlet/loop.sh start
```

`loop.sh` starts components in dependency order:
1. Middleware: Artemis + PostgreSQL (K3s via `middleware.sh`); port-forwards to 12007/12008/12009
2. **Parallel**: ComfyUI (port 12006), tactical LLM server (mlx_lm.server on port 12001)
3. Scorers: clip (12002), artifact (12003), vlm (12004)
4. Spring Boot pipeline (port 12000)

## Stopping the Loop

```bash
~/soxhlet/loop.sh stop
```

## Monitoring

- **Browser UI**: http://localhost:12000  -  real-time image gallery
- **Strategic UI**: http://localhost:12000/strategic/  -  strategic session control
- **Artemis management console**: http://localhost:12009

## Address Topology

```
loop.generate    [anycast / AMQP 1.0]  WorkflowController/ChatWsHandler -> ComfyUiWorker
loop.generated   [anycast / AMQP 1.0]  ComfyUiWorker -> Scorer
loop.verdicts    [anycast / AMQP 1.0]  Scorer (candidates only) -> TacticalLlmCaller
loop.retry       [anycast / AMQP 1.0]  TacticalLlmCaller -> ComfyUiWorker
loop.inpaint     [anycast / AMQP 1.0]  TacticalLlmCaller -> ComfyUiWorker
pipeline.dead    [anycast / AMQP 1.0]  DLX -> (no active consumer)
```

Rejected images (below CLIP or artifact threshold) are written to PostgreSQL but not
published to any queue.

WebSocket gallery events (`image_ready`, `decision`) are broadcast directly in-process
by `GalleryBroadcastService` — they do not go through the broker.

## Threshold Configuration

Thresholds are set in `config.yaml`  -  no rebuild required:

```yaml
thresholds:
  clip:               0.25   # reject if CLIP score < this
  artifact:           0.50   # reject if AI confidence > this
  accept_vlm_mean_min: 7.0   # minimum mean VLM score to pass a candidate to TacticalLlmCaller
  score_timeout_secs: 60
```

See ARCHITECTURE.pdf for calibration procedure.
