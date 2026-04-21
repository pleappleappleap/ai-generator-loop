# Scorers

Parallel image scoring subsystem. Three independent scorers evaluate each
generated image simultaneously. Results are accumulated in SQLite by the Rust
aggregator, which applies cascade threshold logic and emits a verdict.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `clip_scorer.py` | Python (STOMP) | ViT-L-14 CLIP similarity; durable sub to `scorer.requests` |
| `artifact_scorer.py` | Python (STOMP) | AI-image-detector; durable sub to `scorer.requests` |
| `vlm_scorer.py` | Python (STOMP) | Qwen2.5-VL-7B Q5\_K\_M; durable sub to `scorer.requests` |
| `router/` | Rust (AMQP 1.0) | Fans out `loop.events` → `scorer.requests` multicast |
| `aggregator/` | Rust (AMQP 1.0) | Merges results into SQLite `scorer_session`; emits verdicts |
| `coordinator/` | Rust (Unix socket) | XA 2PC log; `BudgetInit/Get/Update`, `SessionInit` API |
| `lancedb_manager/` | Rust (AMQP 1.0) | Writes terminal Loop records to LanceDB |
| `db/` | Rust (library) | Shared SQLite pool, schema, cleanup task |

## Activating the Python Environment

```bash
source ~/ai-image/loop/scorers/activate.sh
```

## Building Rust Binaries

```bash
cd ~/ai-image/loop/scorers
cargo build --release
```

## Running Tests

### Rust
```bash
cd ~/ai-image/loop/scorers
cargo test -p coordinator
```

### Python
```bash
cd ~/ai-image/loop/scorers
source venv/bin/activate
pytest tests/ -v
```

## Adding a New Scorer

1. Create `<name>_scorer.py` following the pattern in `clip_scorer.py`
2. Connect to Artemis via STOMP (`broker.stomp_url` from `config.yaml`)
3. Subscribe to `/topic/scorer.requests` with a durable subscription named
   `scorer.<name>.requests`
4. Subscribe to `/topic/scorer.events` with a durable subscription named
   `scorer.<name>.events` for cancel handling
5. Publish results to `/queue/aggregator.<name>.queue` (anycast)
6. Add the result column to `db/schema.sql` (`scorer_session` table)
7. Add merge logic to `aggregator/src/main.rs`
8. Add the score field to `~/ai-image/lancedb_schema.py`
9. Add tests to `tests/test_<name>_scorer.py`

## Model Storage

```
models/
├── artifact-detector/    umm-maybe/AI-image-detector (HuggingFace)
└── vlm/                  Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf
```
