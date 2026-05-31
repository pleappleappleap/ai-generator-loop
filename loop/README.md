# Loop Subsystem

The generation loop subsystem. Accepts generation requests via the
`loop.request` anycast queue, generates images via ComfyUI, scores them
in parallel, and emits verdicts to the `scorer.result` queue for the
tactical LLM to act on. The browser UI coordinates sessions and displays
generated images in real time.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `ui/server.py` | Python (FastAPI / STOMP) | Browser UI backend; REST + WebSocket gallery; subscribes to `loop.events` and `tactical.decisions` |
| `comfyui_worker.py` | Python (STOMP) | Consumes `loop.request`, registers image with coordinator, drives ComfyUI API, publishes to `loop.events` |
| `monitor.py` | Python (STOMP) | Dead-letter consumer — logs all messages in `pipeline.dead` |
| `scorers/router` | Rust (AMQP 1.0) | Consumes `loop.events`, fans out to `scorer.requests` multicast |
| `scorers/clip_scorer.py` | Python (STOMP) | CLIP semantic similarity scoring |
| `scorers/artifact_scorer.py` | Python (STOMP) | AI artifact detection |
| `scorers/vlm_scorer.py` | Python (STOMP) | VLM holistic image evaluation |
| `scorers/aggregator` | Rust (AMQP 1.0) | Accumulates scorer results in SQLite, emits verdicts, publishes cancels |
| `scorers/coordinator` | Rust (Unix socket) | XA 2PC coordinator; conversation/workflow/budget API |
| `scorers/lancedb_manager` | Rust (AMQP 1.0) | Reads scorer_session from SQLite, writes Loop records to LanceDB |

## Starting the Loop

```bash
~/soxhlet/loop/start_loop.sh
```

`start_loop.sh` starts components in parallel where possible:
1. **Parallel**: ComfyUI, llama.cpp server (tactical LLM backend)
2. Sleep 10 s
3. Coordinator
4. Sleep 1 s
5. Rust workers (router, aggregator, lancedb_manager), Python scorers,
   comfyui_worker, tactical_llm
6. UI served by pipeline (port 12000)
7. Monitor

## Stopping the Loop

```bash
pkill -f "server.py"
pkill -f comfyui_worker
pkill -f clip_scorer
pkill -f artifact_scorer
pkill -f vlm_scorer
pkill -f tactical_llm
pkill -f monitor
pkill -f router
pkill -f aggregator
pkill -f coordinator
pkill -f lancedb_manager
pkill -f "llama.server"
"$ARTEMIS_DATA/bin/artemis" stop
```

## Monitoring

- **Browser UI**: http://localhost:12000 — real-time image gallery
- **Artemis management console**: http://localhost:12009
- **Dead-letter monitor**: `python ~/soxhlet/loop/monitor.py`

## Address Topology

```
loop.request              [anycast  / STOMP]      UI server, tactical_llm → comfyui_worker
loop.events               [multicast / STOMP+AMQP] comfyui_worker → router, UI server
scorer.requests           [multicast / AMQP 1.0]  router → all scorers
scorer.events             [multicast / AMQP 1.0]  aggregator → all scorers, lancedb_manager
aggregator.clip.queue     [anycast  / STOMP+AMQP] clip_scorer → aggregator
aggregator.artifact.queue [anycast  / STOMP+AMQP] artifact_scorer → aggregator
aggregator.vlm.queue      [anycast  / STOMP+AMQP] vlm_scorer → aggregator
scorer.result             [anycast  / AMQP 1.0]   aggregator → tactical_llm
loop.accepted             [multicast / STOMP+AMQP] tactical_llm → lancedb_manager
tactical.decisions        [multicast / STOMP]      tactical_llm → UI server
pipeline.dead             [anycast  / AMQP 1.0]   DLX → monitor
```

## Threshold Configuration

Thresholds are set in `config.yaml` — no rebuild required:

```yaml
thresholds:
  clip:               0.25   # reject if CLIP score < this
  artifact:           0.50   # reject if AI confidence > this
  score_timeout_secs: 60
```

See ARCHITECTURE.pdf for calibration procedure.
