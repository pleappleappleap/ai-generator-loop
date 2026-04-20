# Loop Subsystem

The generation loop subsystem. Accepts generation requests via the
`loop.request` queue, generates images via ComfyUI, scores them in
parallel, and emits verdicts to the `scorer.result` queue for the
tactical LLM to act on.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `comfyui_worker.py` | Python | Consumes `loop.request`, drives ComfyUI API, publishes to `loop.events` |
| `scorers/router` | Rust | Consumes `loop.events`, fans out to `scorer.requests` topic |
| `scorers/clip_scorer.py` | Python | CLIP semantic similarity scoring |
| `scorers/artifact_scorer.py` | Python | AI artifact detection |
| `scorers/vlm_scorer.py` | Python | VLM holistic image evaluation |
| `scorers/aggregator` | Rust | Aggregates scorer results, emits verdicts, publishes cancels |
| `scorers/lancedb_manager.py` | Python | Writes generation records to LanceDB |

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
pkill -f scorer_aggregator
pkill -f lancedb_manager
pkill -f router
pkill -f aggregator
rabbitmqctl stop
redis-cli shutdown
```

## Monitoring

RabbitMQ management UI: http://localhost:15672 (guest/guest)

```bash
redis-cli keys "agg:session:*"   # aggregator in-flight sessions
redis-cli keys "ldb:session:*"   # lancedb manager in-flight sessions
```

## Queue Topology

See ~/ai-image/MESSAGES.md for full message schemas.

```
loop.request          [queue]  → comfyui_worker
loop.events           [topic]  → router
scorer.requests       [topic]  → clip, artifact, vlm scorers
scorer.events         [topic]  → all scorers (cancel)
scorer.results        [topic]  → aggregator, lancedb_manager
scorer.result         [queue]  → tactical LLM
loop.accepted         [topic]  → lancedb_manager
```

## Threshold Configuration

```python
CLIP_THRESHOLD = 0.25      # minimum CLIP similarity score
ARTIFACT_THRESHOLD = 0.5   # maximum AI artifact confidence
```

Placeholder values requiring calibration. See ARCHITECTURE.pdf.
