# AI Image Generation Pipeline

An autonomous SDXL image generation pipeline. Submit a natural language prompt;
the pipeline generates images, scores them across three independent dimensions,
and feeds the results to a tactical LLM that decides whether to accept the
image, revise the prompt, schedule targeted inpainting, or give up. Every
generated image — including rejects — is stored as a vector embedding in
LanceDB, giving the system long-term memory across sessions.

All inter-component communication flows through ActiveMQ Artemis (AMQP 1.0
for Rust components, STOMP for Python). There is no Redis, no RabbitMQ, and
no external database server. Operational state lives in a WAL-mode SQLite
database; long-term history lives in LanceDB.

---

## Architecture Documentation

The authoritative design document is `ARCHITECTURE.pdf`. It covers the
component diagram, full address and queue topology, SQLite schema, XA 2-phase
commit protocol, memory budget, threshold calibration procedure, and scale-out
path.

Generate it from source:

```bash
make doc          # requires MacTeX: brew install --cask mactex
open ARCHITECTURE.pdf
```

The LaTeX source is `ARCHITECTURE.tex` and is version-controlled alongside
the code.

---

## Installation

Full step-by-step instructions are in **`INSTALL.md`**. The summary:

1. **Hardware** — 64 GB+ unified memory (96 GB recommended), ~100 GB free disk.
2. **System software** — Python 3.11, Rust (rustup), yq, ActiveMQ Artemis.
   Run `make prereqs-system` to install automatically.
3. **Three Python venvs** — root (LanceDB, CLIP, stomp.py), scorers
   (transformers, llama-cpp-python, stomp.py), and ComfyUI. Run
   `make prereqs-python` to create all three.
4. **Models** — SDXL checkpoint into ComfyUI (manual); artifact detector, VLM
   (Qwen2.5-VL-7B Q5\_K\_M), and tactical LLM (Qwen3-72B Q4\_K\_M) via
   `make models` or `python model_picker.py`.
5. **Broker** — create an Artemis instance, enable STOMP (61613) and AMQP 1.0
   (5672) acceptors.
6. **Configuration** — `cp config.yaml.default config.yaml`, then
   `python menuconfig.py`.
7. **Build** — `cargo build --release` in `loop/scorers/`.

---

## Quick Start

Once installed (see `INSTALL.md`):

```bash
# Start the broker
~/ai-image/loop/start_broker.sh

# Start the full pipeline
~/ai-image/loop/start_loop.sh

# Submit a generation session
source venv/bin/activate
python tactical-llm/session.py \
  --prompt "two people in a park, photorealistic, golden hour" \
  --max-retries 3 \
  --monitor
```

The `--monitor` flag polls the coordinator for live budget state and prints
progress until the session resolves.

---

## Components

| Component | Language | Role |
|-----------|----------|------|
| `comfyui_worker.py` | Python / STOMP | Drives ComfyUI; registers images with coordinator |
| `clip_scorer.py` | Python / STOMP | ViT-L-14 CLIP semantic similarity |
| `artifact_scorer.py` | Python / STOMP | AI-image-detector artifact confidence |
| `vlm_scorer.py` | Python / STOMP | Qwen2.5-VL-7B holistic image evaluation |
| `router` | Rust / AMQP 1.0 | Fans out `loop.events` → `scorer.requests` |
| `aggregator` | Rust / AMQP 1.0 | Merges scorer results; applies threshold logic; emits verdicts |
| `coordinator` | Rust / Unix socket | XA 2PC log; budget API for Python processes |
| `lancedb_manager` | Rust / AMQP 1.0 | Writes terminal Loop records to LanceDB |
| `tactical_llm.py` | Python / STOMP | Receives verdicts; runs local LLM; decides next action |
| `session.py` | Python / STOMP | Session entry point; initialises budget; publishes first request |
| `monitor.py` | Python / STOMP | Dead-letter consumer; logs `pipeline.dead` messages |

---

## Repository Layout

```
~/ai-image/
├── INSTALL.md              Full installation guide
├── ARCHITECTURE.tex        LaTeX source → ARCHITECTURE.pdf (make doc)
├── MESSAGES.md             Message schema contracts for all addresses
├── config.yaml             Runtime configuration (gitignored)
├── config.yaml.default     Configuration template
├── config.py               Python config loader
├── lancedb_schema.py       LanceDB table definitions (Pydantic)
├── menuconfig.py           Interactive TUI configuration editor
├── model_picker.py         HuggingFace model browser (curses TUI or URL mode)
├── check_env.py            Dependency and environment checker
├── loop/
│   ├── comfyui_worker.py
│   ├── monitor.py
│   ├── start_broker.sh
│   ├── start_loop.sh
│   └── scorers/
│       ├── clip_scorer.py
│       ├── artifact_scorer.py
│       ├── vlm_scorer.py
│       ├── router/
│       ├── aggregator/
│       ├── coordinator/
│       ├── db/
│       └── lancedb_manager/
└── tactical-llm/
    ├── session.py
    ├── tactical_llm.py
    ├── prompts.py
    ├── retrieval.py
    └── tests/
```

---

## Monitoring

- **Artemis console** — `http://localhost:8161` (admin / admin by default)
- **Dead-letter queue** — `python loop/monitor.py` streams all failed messages
- **Message contracts** — `MESSAGES.md` documents every address, routing type,
  protocol, and payload schema

## Tests

```bash
# Rust (coordinator XA state machine, budget ops, crash recovery)
cd loop/scorers && cargo test -p coordinator

# Python (tactical LLM decisions, retrieval, prompt construction)
cd tactical-llm && python -m pytest tests/ -v
```
