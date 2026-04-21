# AI Image Generation Pipeline

An autonomous image generation pipeline using Stable Diffusion XL,
multi-dimensional scoring, and a tactical LLM feedback loop to iteratively
refine generated images toward a quality target.

## Overview

The pipeline accepts a natural language prompt and autonomously generates,
scores, and refines images until a candidate meeting all quality thresholds
is accepted. A tactical LLM interprets scorer feedback and decides whether
to accept a candidate, modify the prompt, adjust generation parameters, or
trigger targeted inpainting on specific regions.

All generation history — including rejected candidates — is stored in
LanceDB as vector embeddings alongside full scorer results and generation
parameters. This long-term memory enables the strategic LLM to identify
patterns across sessions and improve generation quality over time.

## Directory Structure

```
~/ai-image/
├── ARCHITECTURE.tex        LaTeX source for system architecture document
├── ARCHITECTURE.pdf        Rendered architecture document (make doc)
├── Makefile                Top-level build orchestration
├── config.yaml             Single source of truth for all configuration
├── config.yaml.default     Template for config.yaml
├── config.py               Python config loader (serde_yaml equivalent)
├── lancedb_schema.py       LanceDB table schema definitions (Pydantic)
├── menuconfig.py           Interactive TUI config editor
├── check_env.py            Dependency and environment checker
├── loop/
│   ├── ComfyUI/            Stable Diffusion XL generation engine
│   ├── comfyui_worker.py   STOMP consumer wrapping the ComfyUI API
│   ├── monitor.py          Dead-letter consumer for pipeline.dead queue
│   ├── start_broker.sh     Starts ActiveMQ Artemis
│   ├── start_loop.sh       Starts all loop infrastructure
│   └── scorers/
│       ├── clip_scorer.py       CLIP semantic similarity scorer (STOMP)
│       ├── artifact_scorer.py   AI artifact detection scorer (STOMP)
│       ├── vlm_scorer.py        VLM holistic image evaluator (STOMP)
│       ├── Cargo.toml           Rust workspace root
│       ├── router/              Generation event fanout (AMQP 1.0)
│       ├── aggregator/          Scorer result collection and verdict (AMQP 1.0)
│       ├── coordinator/         XA coordinator + Python budget API (Unix socket)
│       ├── db/                  Shared SQLite helpers (WAL, schema, cleanup)
│       └── lancedb_manager/     Terminal event LanceDB writer (AMQP 1.0)
├── tactical-llm/
│   ├── session.py          Session entry point (STOMP publish + budget init)
│   ├── tactical_llm.py     Tactical decision engine (STOMP consumer)
│   ├── prompts.py          System prompt and decision prompt construction
│   ├── retrieval.py        LanceDB retrieval helpers (session history + ANN)
│   └── tests/              pytest test suite
├── lancedb/                LanceDB persistent storage (sessions, loop tables)
└── strategic-llm/          Strategic LLM subsystem (planned)
```

## Prerequisites

### Hardware
- Apple Silicon Mac with at least 64 GB unified memory (96 GB recommended)
- ~100 GB free disk space for models and generation output

### Software
- macOS 14 or later
- Homebrew
- Python 3.11+
- Rust (installed via rustup)
- yq (`brew install yq`) — YAML config reader for shell scripts
- ActiveMQ Artemis — message broker (AMQP 1.0 + STOMP)
- MacTeX (for architecture documentation: `brew install --cask mactex`)

### Python packages
- Root venv: `lancedb`, `open_clip_torch`, `torch`, `stomp.py`
- Scorers venv: `transformers`, `llama-cpp-python`, `stomp.py`, `open_clip_torch`

### Models
See `loop/README.md` for the full model download procedure.

## Configuration

All configuration lives in `config.yaml`. Copy the default template and edit:

```bash
cp config.yaml.default config.yaml
python menuconfig.py   # interactive TUI editor
```

Key sections:

```yaml
broker:
  rabbitmq_url: amqp://user:pass@localhost:5672   # Artemis AMQP 1.0 URL
  stomp_url:    stomp://user:pass@localhost:61613  # Artemis STOMP URL
  artemis_data: /path/to/artemis-broker           # broker instance directory

database:
  path:                  pipeline.db
  busy_timeout_ms:       5000
  cleanup_interval_secs: 300

thresholds:
  clip:               0.25
  artifact:           0.50
  score_timeout_secs: 60
```

## Quick Start

```bash
# 1. Build Rust binaries
cd ~/ai-image/loop/scorers
cargo build --release

# 2. Start the Artemis broker
~/ai-image/loop/start_broker.sh

# 3. Start the pipeline
~/ai-image/loop/start_loop.sh

# 4. Submit a generation session
cd ~/ai-image
python tactical-llm/session.py \
  --prompt "two people in a park, photorealistic, golden hour" \
  --max-retries 3 \
  --monitor
```

The `--monitor` flag polls the coordinator for budget state and prints
progress until the session resolves.

## Running Tests

### Rust (coordinator)
```bash
cd loop/scorers
cargo test -p coordinator
```

### Python (tactical-llm)
```bash
cd tactical-llm
python -m pytest tests/ -v
```

## Monitoring

- Artemis management console: `http://localhost:8161` (admin/admin by default)
- Dead-letter queue: `pipeline.dead` — start `python loop/monitor.py` to watch

## Documentation

```bash
make doc        # renders ARCHITECTURE.pdf
```

See `ARCHITECTURE.pdf` for full system design including component diagram,
address topology, SQLite schema, XA 2PC protocol, and memory budget.
