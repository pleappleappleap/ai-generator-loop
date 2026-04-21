# Loop Subsystem

The generation loop subsystem. Accepts generation requests via the
`loop.request` anycast queue, generates images via ComfyUI, scores them
in parallel, and emits verdicts to the `scorer.result` queue for the
tactical LLM to act on.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `comfyui_worker.py` | Python (STOMP) | Consumes `loop.request`, registers image with coordinator, drives ComfyUI API, publishes to `loop.events` |
| `monitor.py` | Python (STOMP) | Dead-letter consumer — logs all messages in `pipeline.dead` |
| `scorers/router` | Rust (AMQP 1.0) | Consumes `loop.events`, fans out to `scorer.requests` multicast |
| `scorers/clip_scorer.py` | Python (STOMP) | CLIP semantic similarity scoring |
| `scorers/artifact_scorer.py` | Python (STOMP) | AI artifact detection |
| `scorers/vlm_scorer.py` | Python (STOMP) | VLM holistic image evaluation |
| `scorers/aggregator` | Rust (AMQP 1.0) | Accumulates scorer results in SQLite, emits verdicts, publishes cancels |
| `scorers/coordinator` | Rust (Unix socket) | XA 2PC coordinator; Python budget API |
| `scorers/lancedb_manager` | Rust (AMQP 1.0) | Reads scorer_session from SQLite, writes Loop records to LanceDB |

## Starting the Loop

```bash
~/ai-image/loop/start_loop.sh
```

## Stopping the Loop

```bash
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
"$ARTEMIS_DATA/bin/artemis" stop
```

## Monitoring

Artemis management console: http://localhost:8161

Dead-letter messages land in `pipeline.dead`. Start the monitor to watch:

```bash
python ~/ai-image/loop/monitor.py
```

## Address Topology

```
loop.request             [anycast  / STOMP]    session.py, tactical_llm → comfyui_worker
loop.events              [multicast / STOMP+AMQP] comfyui_worker → router
scorer.requests          [multicast / AMQP 1.0]  router → all scorers
scorer.events            [multicast / AMQP 1.0]  aggregator → all scorers, lancedb_manager
aggregator.clip.queue    [anycast  / STOMP+AMQP] clip_scorer → aggregator
aggregator.artifact.queue [anycast / STOMP+AMQP] artifact_scorer → aggregator
aggregator.vlm.queue     [anycast  / STOMP+AMQP] vlm_scorer → aggregator
scorer.result            [anycast  / AMQP 1.0]   aggregator → tactical_llm
loop.accepted            [multicast / STOMP+AMQP] tactical_llm → lancedb_manager
pipeline.dead            [anycast  / AMQP 1.0]   DLX → monitor
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
