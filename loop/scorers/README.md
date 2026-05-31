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
| `router/` | Rust (AMQP 1.0) | Fans out `loop.events` -> `scorer.requests` multicast |
| `aggregator/` | Rust (AMQP 1.0) | Merges results into SQLite `scorer_session`; emits verdicts |
| `coordinator/` | Rust (Unix socket) | XA 2PC log; conversation/workflow/budget API |
| `lancedb_manager/` | Rust (AMQP 1.0) | Writes terminal Loop records to LanceDB |
| `db/` | Rust (library) | Shared SQLite pool, schema, cleanup task |

The scorers Python venv (`venv/`) is also used by `tactical_llm.py`.

## Activating the Python Environment

```bash
source ~/soxhlet/loop/scorers/activate.sh
```

## Building Rust Binaries

```bash
cd ~/soxhlet/loop/scorers
~/.cargo/bin/cargo build --release
```

## Running Tests

### Rust
```bash
cd ~/soxhlet/loop/scorers
~/.cargo/bin/cargo test -p coordinator
```

### Python
```bash
cd ~/soxhlet/loop/scorers
venv/bin/pytest tests/ -v
```

The `tests/` directory covers:
- `test_clip_scorer.py`  -  CLIP scorer unit tests
- `test_artifact_scorer.py`  -  artifact scorer unit tests
- `test_vlm_scorer.py`  -  VLM scorer unit tests
- `test_ui_server.py`  -  UI server REST, WebSocket, and STOMP listener tests

### Full suite (lint + typecheck + Rust + Python)
```bash
cd ~/soxhlet/loop
make all
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
8. Add the score field to `~/soxhlet/lancedb_schema.py`
9. Add tests to `tests/test_<name>_scorer.py`

## Model Storage

```
models/
+-- artifact-detector/    umm-maybe/AI-image-detector (HuggingFace)
+-- vlm/                  Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf
+-- tactical/             Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-4bit/ (served via mlx_lm.server)
\-- strategic/            Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated/ (served via mlx_lm.server)
```
