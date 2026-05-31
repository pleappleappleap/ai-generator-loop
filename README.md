# Soxhlet

An autonomous image generation pipeline with a browser UI. Open the UI,
enter a prompt, and watch the pipeline generate images, score them across three
independent dimensions, and feed the results to a tactical LLM that decides
whether to accept the image, revise the prompt, schedule targeted inpainting,
or give up. Every generated image (including rejects) is stored with its
embeddings for long-term memory across sessions.

Sessions are organised into **conversations** (named projects) and
**workflows** (individual generation runs). The browser UI manages this
hierarchy directly; the tactical LLM receives conversation history and user
ratings to inform its decisions.

The system is organised into two mutually exclusive **super-components** plus
shared middleware:

| Super-component | Contents | Lifecycle |
|----------------|----------|-----------|
| **Middleware** | Artemis (K3s), PostgreSQL (K3s) | Always on; persists across mode switches |
| **Loop** | ComfyUI, scorers, tactical LLM, pipeline | Started with `loop.sh start` |
| **Strategic** | Strategic LLM, pipeline | Started with `strategic.sh start` |

Loop and Strategic share the Spring Boot pipeline but cannot run simultaneously  - 
all available GPU memory is concentrated on the active super-component.

All inter-component communication flows through ActiveMQ Artemis (AMQP 1.0).
Operational state lives in PostgreSQL (Flyway-managed; pgvector extension for
CLIP embeddings and north-star similarity queries).

---

## Architecture Documentation

The authoritative design document is `ARCHITECTURE.pdf`. It covers the
component diagram, full address and queue topology, PostgreSQL schema,
Narayana JTA transaction boundaries, conversation and workflow hierarchy,
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
2. **System software**: Python 3.11, Java 21+, yq, K3s (or any K8s),
   `kubectl`. Run `make prereqs-system` to install automatically.
3. **Three Python venvs**: root (CLIP, pgvector client), scorers
   (transformers, mlx-lm, fastapi, uvicorn, httpx), and
   ComfyUI. Run `make prereqs-python` to create all three.
4. **Models**: SDXL checkpoint into ComfyUI (manual); artifact detector, VLM
   (Qwen3-VL-8B-Instruct Q8 GGUF), tactical LLM (Qwen3-Next-80B-A3B MLX 4-bit), and
   strategic LLM (Qwen3-Next-80B-A3B-Thinking bf16) via `make models`.
5. **Middleware**: `middleware.sh start`  -  starts Artemis and PostgreSQL in K3s
   and establishes port-forwards (12007/12008/12009).
6. **Configuration**: `cp config.yaml.default config.yaml`, then
   `python menuconfig.py`.
7. **Build**: `mvn package` in `pipeline/`.

---

## Quick Start

Once installed (see `INSTALL.md`):

```bash
# Start middleware (Artemis + PostgreSQL)
~/soxhlet/middleware.sh start

# Start the full Loop super-component
~/soxhlet/loop.sh start

# Open the browser UI
open http://localhost:12000

# Open the strategic review UI (when in strategic mode)
open http://localhost:12000/strategic/
```

Create a conversation, start a workflow, and submit a prompt from the UI.
The gallery updates in real time as images are generated and scored.

---

## Components

| Component | Language | Role |
|-----------|----------|------|
| `pipeline` | Java / Spring Boot | REST + WebSocket UI backend; drives ComfyUI; calls scorer sidecars; tactical LLM agentic loop; strategic session handler |
| `clip_scorer.py` | Python / FastAPI | ViT-L-14 CLIP semantic similarity; `/score` + `/embed_text` |
| `artifact_scorer.py` | Python / FastAPI | AI-image-detector artifact confidence; `/score` |
| `vlm_scorer.py` | Python / FastAPI | Qwen3-VL-8B holistic image evaluation; `/score` + `/analyze` |
| `tactical LLM server` | Python / mlx_lm | Serves Qwen3-Next-80B-A3B (MLX 4-bit) on port 12001 |
| `strategic LLM server` | Python / mlx_lm | Serves Qwen3-Next-80B-A3B-Thinking (bf16) on port 12005 |

---

## Repository Layout

```
~/soxhlet/
+-- loop.sh                 Loop super-component manager (start/stop/status/health)
+-- middleware.sh           Middleware manager (Artemis + PostgreSQL via K3s + port-forwards)
+-- strategic.sh            Strategic super-component manager
+-- INSTALL.md              Full installation guide
+-- ARCHITECTURE.tex        LaTeX source -> ARCHITECTURE.pdf (make doc)
+-- MESSAGES.md             Message schema contracts for all addresses
+-- config.yaml.default     Configuration template (cp to config.yaml, then edit)
+-- config.py               Python config loader
+-- menuconfig.py           Interactive TUI configuration editor
+-- model_picker.py         HuggingFace model browser
+-- pipeline/               Java Spring Boot pipeline (REST + WebSocket + all JMS workers)
+-- middleware/             K8s manifests for Artemis and PostgreSQL
+-- loop/
|   +-- comfyui.sh          ComfyUI component manager
|   +-- tactical_llm.sh     Tactical LLM server component manager (mlx_lm.server)
|   +-- tactical-llm/       Tactical LLM prompt library + system prompt
|   |   +-- prompts.py
|   |   \-- system_prompt.md
|   +-- scorers/            Python HTTP scorer sidecars
|   |   +-- clip_scorer.py
|   |   +-- artifact_scorer.py
|   |   \-- vlm_scorer.py
|   \-- workflows/          ComfyUI workflow JSON files
\-- strategic-llm/          Strategic LLM component scripts + system prompt
    +-- strategic_llm.sh    Strategic LLM server component manager (mlx_lm.server)
    \-- system_prompt.md
```

---

## Monitoring

- **Browser UI**: `http://localhost:12000`  -  real-time image gallery and session control
- **Strategic UI**: `http://localhost:12000/strategic/`  -  strategic session control
- **Artemis console**: `http://localhost:12009` (admin / admin by default)
- **Dead-letter queue**: Artemis console at `http://localhost:12009` → Queues → pipeline.dead
- **Message contracts**: `MESSAGES.md` documents every address, protocol, publisher, consumer, and payload schema

## Tests

```bash
make test     # all Python tests
make all      # tests + lint + typecheck + build
```
