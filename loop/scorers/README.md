# Scorers

Parallel image scoring subsystem. Three independent scorers evaluate
each generated image simultaneously. Results are aggregated by the
Rust aggregator which applies cascade threshold logic and emits a
verdict.

## Components

| Scorer | Model | Routing Key |
|--------|-------|-------------|
| `clip_scorer.py` | ViT-L-14 (CLIP) | `clip.<uuid>` |
| `artifact_scorer.py` | umm-maybe/AI-image-detector | `artifact.<uuid>` |
| `vlm_scorer.py` | Qwen2.5-VL-7B Q5_K_M | `vlm.<uuid>` |
| `router/` (Rust) | — | Routes loop.events → scorer.requests |
| `aggregator/` (Rust) | — | Aggregates results, emits verdicts |

## Activating the Python Environment

```bash
source ~/ai-image/loop/scorers/activate.sh
```

## Running Tests

```bash
cd ~/ai-image/loop/scorers
source venv/bin/activate
pytest tests/ -v
```

## Adding a New Scorer

1. Create `<name>_scorer.py` following the pattern in `clip_scorer.py`
2. Subscribe to `scorer.requests` with binding key `score.*`
3. Subscribe to `scorer.events` with binding key `cancel.*`
4. Publish to `scorer.results` with routing key `<name>.<image_uuid>`
5. Add the score field to `~/ai-image/lancedb_schema.py`
6. Add tests to `tests/test_<name>_scorer.py`

## Model Storage

```
models/
├── artifact-detector/    umm-maybe/AI-image-detector
└── vlm/                  Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf
```
