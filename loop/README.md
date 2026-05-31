# Loop Subsystem

The generation loop subsystem. Accepts generation requests via the
`loop.request` anycast queue, generates images via ComfyUI, scores them
in parallel, and emits verdicts to the `scorer.result` queue for the
tactical LLM to act on. The browser UI coordinates sessions and displays
generated images in real time.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `pipeline` | Java / Spring Boot | Browser UI backend; drives ComfyUI; calls tactical LLM; handles verdicts |
| `scorers/router` | Rust (AMQP 1.0) | Consumes `loop.events`, fans out to `scorer.requests` multicast |
| `scorers/clip_scorer.py` | Python (FastAPI) | CLIP semantic similarity scoring |
| `scorers/artifact_scorer.py` | Python (FastAPI) | AI artifact detection |
| `scorers/vlm_scorer.py` | Python (FastAPI) | VLM holistic image evaluation + analyze endpoint |
| `scorers/aggregator` | Rust (AMQP 1.0) | Accumulates scorer results in SQLite, emits verdicts, publishes cancels |
| `scorers/coordinator` | Rust (Unix socket) | XA 2PC coordinator; conversation/workflow/budget API |
| `scorers/lancedb_manager` | Rust (AMQP 1.0) | Reads scorer_session from SQLite, writes Loop records to LanceDB |

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
loop.request              [anycast  / AMQP 1.0]   pipeline (retry) -> pipeline (ComfyUiWorker)
loop.events               [multicast / AMQP 1.0]  pipeline (ComfyUiWorker) -> router, pipeline (UI)
scorer.requests           [multicast / AMQP 1.0]  router -> all scorers
scorer.events             [multicast / AMQP 1.0]  aggregator -> all scorers, lancedb_manager
aggregator.clip.queue     [anycast  / AMQP 1.0]   clip_scorer -> aggregator
aggregator.artifact.queue [anycast  / AMQP 1.0]   artifact_scorer -> aggregator
aggregator.vlm.queue      [anycast  / AMQP 1.0]   vlm_scorer -> aggregator
scorer.result             [anycast  / AMQP 1.0]   aggregator -> pipeline (TacticalLlmCaller)
loop.accepted             [anycast  / AMQP 1.0]   pipeline (TacticalLlmCaller) -> lancedb_manager
loop.retry                [anycast  / AMQP 1.0]   pipeline (TacticalLlmCaller) -> pipeline (ComfyUiWorker)
tactical.decisions        [multicast / AMQP 1.0]  pipeline (TacticalLlmCaller) -> pipeline (UI WebSocket)
pipeline.dead             [anycast  / AMQP 1.0]   DLX -> (no active consumer)
```

## Threshold Configuration

Thresholds are set in `config.yaml`  -  no rebuild required:

```yaml
thresholds:
  clip:               0.25   # reject if CLIP score < this
  artifact:           0.50   # reject if AI confidence > this
  score_timeout_secs: 60
```

See ARCHITECTURE.pdf for calibration procedure.
