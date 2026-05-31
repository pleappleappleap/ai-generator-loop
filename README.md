# AI Image Generation Pipeline

An autonomous SDXL image generation pipeline with a browser UI. Open the UI,
enter a prompt, and watch the pipeline generate images, score them across three
independent dimensions, and feed the results to a tactical LLM that decides
whether to accept the image, revise the prompt, schedule targeted inpainting,
or give up. Every generated image (including rejects) is stored as a vector
embedding in LanceDB, giving the system long-term memory across sessions.

Sessions are organised into **conversations** (named projects) and
**workflows** (individual generation runs). The browser UI manages this
hierarchy directly; the tactical LLM receives conversation history and user
ratings to inform its decisions.

All inter-component communication flows through ActiveMQ Artemis (AMQP 1.0
for Rust components, STOMP for Python). There is no Redis, no RabbitMQ, and
no external database server. Operational state lives in a WAL-mode SQLite
database; long-term history lives in LanceDB.

---

## Architecture Documentation

The authoritative design document is `ARCHITECTURE.pdf`. It covers the
component diagram, full address and queue topology, SQLite schema, XA 2-phase
commit protocol, conversation and workflow hierarchy, coordinator API,
memory budget, threshold calibration procedure, and scale-out path.

Generate it from source:

```bash
make doc          # requires a TeX distribution (MacTeX, TeX Live, MiKTeX)
open ARCHITECTURE.pdf
```

The LaTeX source is `ARCHITECTURE.tex` and is version-controlled alongside
the code.

---

## Installation

Full step-by-step instructions are in **`INSTALL.md`**. The summary:

1. **Hardware**: 64 GB+ GPU memory (Apple unified memory, CUDA VRAM, or system RAM for CPU), ~100 GB free disk.
2. **System software**: Python 3.11, Rust (rustup), yq, ActiveMQ Artemis.
   Run `make prereqs-system` to install automatically.
3. **Three Python venvs**: root (LanceDB, CLIP, stomp.py), scorers
   (transformers, llama-cpp-python, fastapi, uvicorn, httpx, stomp.py), and
   ComfyUI. Run `make prereqs-python` to create all three.
4. **Models**: SDXL checkpoint into ComfyUI (manual); artifact detector, VLM
   (Qwen2.5-VL-7B Q5\_K\_M), and tactical LLM (Qwen3-72B Q4\_K\_M) via
   `make models` or `python model_picker.py`.
5. **Broker**: create an Artemis instance, enable STOMP (61613) and AMQP 1.0
   (5672) acceptors.
6. **Configuration**: `cp config.yaml.default config.yaml`, then
   `python menuconfig.py`.
7. **Build**: `cargo build --release` in `loop/scorers/`.

---

## Quick Start

Once installed (see `INSTALL.md`):

```bash
# Start the broker
~/soxhlet/loop/start_broker.sh

# Start the full pipeline
~/soxhlet/loop/start_loop.sh

# Open the browser UI
open http://localhost:7860
```

Create a conversation, start a workflow, and submit a prompt from the UI.
The gallery updates in real time as images are generated and scored.

---

## Components

| Component | Language | Role |
|-----------|----------|------|
| `server.py` | Python / FastAPI | Browser UI backend; REST + WebSocket gallery; STOMP subscriber |
| `comfyui_worker.py` | Python / STOMP | Drives ComfyUI; registers images with coordinator |
| `clip_scorer.py` | Python / STOMP | ViT-L-14 CLIP semantic similarity |
| `artifact_scorer.py` | Python / STOMP | AI-image-detector artifact confidence |
| `vlm_scorer.py` | Python / STOMP | Qwen2.5-VL-7B holistic image evaluation |
| `router` | Rust / AMQP 1.0 | Fans out `loop.events` → `scorer.requests` |
| `aggregator` | Rust / AMQP 1.0 | Merges scorer results; applies threshold logic; emits verdicts |
| `coordinator` | Rust / Unix socket | XA 2PC log; conversation/workflow/budget API for Python processes |
| `lancedb_manager` | Rust / AMQP 1.0 | Writes terminal Loop records to LanceDB |
| `tactical_llm.py` | Python / STOMP | Receives verdicts; calls llama.cpp server; decides next action |
| `llama.cpp server` | C++ / HTTP | Serves Qwen3-72B Q4\_K\_M via OpenAI-compatible REST API |
| `monitor.py` | Python / STOMP | Dead-letter consumer; logs `pipeline.dead` messages |

---

## Repository Layout

```
~/soxhlet/
├── INSTALL.md              Full installation guide
├── ARCHITECTURE.tex        LaTeX source → ARCHITECTURE.pdf (make doc)
├── MESSAGES.md             Message schema contracts for all addresses
├── config.yaml.default     Configuration template (cp to config.yaml, then edit)
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
│   ├── ui/
│   │   ├── server.py       FastAPI UI backend (port 7860)
│   │   ├── static/         HTML/CSS/JS served by server.py
│   │   └── pyrightconfig.json
│   └── scorers/
│       ├── clip_scorer.py
│       ├── artifact_scorer.py
│       ├── vlm_scorer.py
│       ├── tests/
│       ├── router/
│       ├── aggregator/
│       ├── coordinator/
│       ├── db/
│       └── lancedb_manager/
└── tactical-llm/
    ├── tactical_llm.py
    ├── prompts.py
    ├── retrieval.py
    └── tests/
```

---

## Monitoring

- **Browser UI**: `http://localhost:7860` — real-time image gallery and session control
- **Artemis console**: `http://localhost:8161` (admin / admin by default)
- **Dead-letter queue**: `python loop/monitor.py` streams all failed messages
- **Message contracts**: `MESSAGES.md` documents every address, routing type,
  protocol, and payload schema

## Tests

```bash
make test     # all Rust + Python tests
make all      # tests + lint + typecheck + build
```
