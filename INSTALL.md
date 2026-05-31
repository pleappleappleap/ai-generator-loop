# Installation Guide

Step-by-step setup for the AI image generation pipeline.
For architecture and design documentation see `ARCHITECTURE.pdf` (`make doc`).

---

## 1. Hardware Requirements

Choose the platform that matches your hardware:

| Platform | Requirement | Minimum | Recommended |
|----------|-------------|---------|-------------|
| Apple Silicon (MPS) | Unified memory | 64 GB | 96 GB |
| NVIDIA (CUDA) | GPU VRAM | 24 GB | 48 GB+ |
| NVIDIA (CUDA) | System RAM | 32 GB | 64 GB |
| AMD (ROCm) | GPU VRAM | 24 GB | 48 GB+ |
| AMD (ROCm) | System RAM | 32 GB | 64 GB |
| CPU only | System RAM | 64 GB | 128 GB+ |
| All platforms | Free disk | 100 GB | 200 GB |

CPU-only mode is supported but very slow. Expect hours per image rather than
minutes. On discrete-GPU systems with insufficient VRAM, the tactical LLM can
be partially offloaded to CPU via `tactical.model.n_gpu_layers` in
`config.yaml`.

---

## 2. Bootstrap Prerequisites

These must be in place **before** you can run `make`. Everything else
(`make prereqs-system`) installs automatically from here.

### macOS

**Xcode Command Line Tools** (provides git, make, clang, curl):

```bash
xcode-select --install
```

**Homebrew** (the macOS package manager):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the post-install instructions to add Homebrew to your PATH.

### Linux

Most distributions ship git and make. If not:

| Distro | Command |
|--------|---------|
| Debian / Ubuntu | `sudo apt install git make` |
| Fedora / RHEL | `sudo dnf install git make` |
| Arch | `sudo pacman -S git make` |
| openSUSE | `sudo zypper install git make` |

### Windows

1. **Git Bash** — install from https://git-scm.com/downloads. This provides
   bash, git, make, curl, and wget in one package. Run all `make` commands
   from the Git Bash terminal, not CMD or PowerShell.

2. **winget** — ships with Windows 11. On Windows 10, install the
   [App Installer](https://apps.microsoft.com/detail/9NBLGGH4NNS1) from the
   Microsoft Store. `make prereqs-system` uses winget to install Docker.

### All platforms — clone the repository

```bash
git clone <repo-url> ~/soxhlet
cd ~/soxhlet
```

Add `SOXHLET_ROOT` to your shell profile so the pipeline scripts can locate
`config.yaml` from any working directory:

```bash
export SOXHLET_ROOT=~/soxhlet
```

---

## 3. System Prerequisites

With the bootstrap in place, run:

```bash
make prereqs-system
```

This installs automatically, per platform:

| Tool | macOS | Linux | Notes |
|------|-------|-------|-------|
| wget | — | ✓ | macOS uses curl (system native) |
| Python 3.11 | ✓ | ✓ | via Homebrew / distro package manager |
| yq | ✓ | ✓ | YAML query tool used by shell scripts |
| Docker | ✓ | ✓ | Desktop (macOS/Windows) or Engine (Linux) |
| Rust + cargo | ✓ | ✓ | via rustup |
| sqlx-cli | ✓ | ✓ | Rust SQLite compile-time query verification |
| protoc | ✓ | ✓ | required by LanceDB Rust crate |

TeX is **not** installed automatically. It is only needed to regenerate
`ARCHITECTURE.pdf`:

| Platform | Command |
|----------|---------|
| macOS | `brew install --cask mactex` |
| Linux | `sudo apt install texlive-full latexmk` (or equivalent) |
| Windows | Install MiKTeX from https://miktex.org/download |

---

## 4. ActiveMQ Artemis (Docker)

The pipeline runs Artemis via the official Docker image. No manual broker
instance creation or `broker.xml` editing is required — AMQP (5672), STOMP
(61613), and the management console (8161) are pre-enabled in the image.

### Install Docker

| Platform | Installed by |
|----------|-------------|
| macOS | `make prereqs-system` — installs `colima` + `docker` CLI via Homebrew, then starts colima |
| Linux | `make prereqs-system` — runs the official get.docker.com script, enables the systemd service |
| Windows | `make prereqs-system` — installs Docker Desktop via winget |

On macOS the pipeline uses **colima** (a lightweight, headless Docker daemon) rather than
Docker Desktop. No GUI, no licence agreement, no sudo required — just `brew install colima docker`.

### Pull the image

`make all` pulls the image automatically. To pull it on its own:

```bash
make artemis-broker
```

This is idempotent — subsequent runs do nothing if the image is already
present.

### Start the broker

```bash
loop/start_broker.sh
```

Broker data is persisted in `loop/artemis-data/` (bind-mounted into the
container), so queue state survives container restarts.

### Management console

`http://localhost:8161` — credentials: **admin / admin**

---

## 5. Python Environments

There are **three** separate virtual environments:

- **Root venv** (`venv/`): used by utilities such as `check_env.py` and `model_picker.py`.
- **Scorers venv** (`loop/scorers/venv/`): used by all scorer processes,
  `tactical_llm.py`, and the UI server (`server.py`), which share large ML
  libraries and the FastAPI/uvicorn stack.
- **ComfyUI venv** (`loop/ComfyUI/venv/`): used exclusively by ComfyUI,
  managed by `make prereqs`.

### Root environment

```bash
cd $SOXHLET_ROOT
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

### Scorers environment

Install `llama-cpp-python` with the correct GPU backend flag for your
platform before installing the remaining requirements:

| Platform | CMAKE_ARGS |
|----------|------------|
| macOS (Metal / MPS) | `CMAKE_ARGS="-DLLAMA_METAL=on"` |
| NVIDIA (CUDA) | `CMAKE_ARGS="-DGGML_CUDA=on"` |
| AMD (ROCm / HIP) | `CMAKE_ARGS="-DGGML_HIPBLAS=on"` |
| CPU only | *(no flag required)* |

```bash
cd $SOXHLET_ROOT/loop/scorers
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Set CMAKE_ARGS per the table above, then:
pip install llama-cpp-python --no-binary llama-cpp-python

pip install -r requirements.txt
deactivate
```

The scorers `requirements.txt` includes the UI server dependencies:
`fastapi`, `uvicorn[standard]`, `httpx`, `openai`, and `starlette`.

To activate the scorers environment in a shell, use the provided script:

```bash
. $SOXHLET_ROOT/loop/scorers/activate.sh
```

---

## 6. Model Downloads

Run `make models` to download everything automatically. The manual steps
below are for reference or if you need to download individual models.

All models are stored under `loop/scorers/models/`.

### SDXL checkpoint (ComfyUI)

Download your preferred SDXL checkpoint (e.g., Absolute Reality XL) and
place it in:

```
loop/ComfyUI/models/checkpoints/
```

The workflow JSON files under `loop/workflows/` reference the checkpoint by
filename. Update `config.yaml` → `models.checkpoint` if you use a
non-default filename.

### AI artifact detector

```bash
cd $SOXHLET_ROOT/loop/scorers
source venv/bin/activate
python - <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="umm-maybe/AI-image-detector",
    local_dir="models/artifact-detector"
)
EOF
```

### VLM scorer (Qwen2.5-VL-7B)

```bash
mkdir -p $SOXHLET_ROOT/loop/scorers/models/vlm
# ~5 GB download
curl -L -o $SOXHLET_ROOT/loop/scorers/models/vlm/Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf \
  "https://huggingface.co/bartowski/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf"
```

### Tactical LLM (Qwen3-72B)

Qwen3-72B is a hybrid thinking model: it reasons step-by-step through
ambiguous decisions and responds directly for clear-cut cases, controlled
by a `<think>` token in the prompt.

The tactical LLM runs as a **llama.cpp HTTP server** (started automatically
by `start_loop.sh`). The `tactical_llm.py` Python process calls it via the
OpenAI-compatible REST API.

```bash
mkdir -p $SOXHLET_ROOT/loop/scorers/models/tactical
# ~43 GB download
curl -L -o $SOXHLET_ROOT/loop/scorers/models/tactical/Qwen3-72B-abliterated-Q4_K_M.gguf \
  "https://huggingface.co/bartowski/Qwen3-72B-abliterated-GGUF/resolve/main/Qwen3-72B-abliterated-Q4_K_M.gguf"
```

Verify the filenames match `config.yaml.default` → `models.vlm.filename` and
`tactical.model.filename`, or update `config.yaml` to match what you
downloaded.

### Automated download and model selection

`make models` downloads the artifact detector, VLM, and tactical LLM
automatically using filenames from `config.yaml`:

```bash
make models
```

To browse HuggingFace and select a different model interactively, use the
model picker. Without an API token it prints browse URLs and drop locations;
with `HF_TOKEN` set it launches a curses TUI for searching and selecting:

```bash
source venv/bin/activate
python model_picker.py                   # no token: URLs + drop paths
HF_TOKEN=hf_... python model_picker.py   # with token: interactive TUI
```

The TUI writes the chosen filename directly to `config.yaml`.

---

## 7. ComfyUI

`make all` handles ComfyUI setup fully automatically:

- Clones ComfyUI into `loop/ComfyUI/` and installs its venv
- Bootstraps ComfyUI-Manager via `cm-cli.py`
- Installs custom nodes: **ComfyUI-Inpaint-Nodes** (`inpaint-nodes`) and
  **comfyui_segment_anything** (`sam`)

Custom nodes are installed by cloning their repos directly into
`loop/ComfyUI/custom_nodes/`:

| Node | Source |
|------|--------|
| `comfyui-inpaint-nodes` | github.com/Acly/comfyui-inpaint-nodes |
| `comfyui_segment_anything` | github.com/storyicon/comfyui_segment_anything |

To run just the custom node step on its own:

```bash
make comfyui-nodes
```

### Optional vision models (for inpainting)

The SAM and Grounding DINO model weights are not downloaded automatically
(large files, inpainting-only). Download them if you intend to use inpainting:

```bash
# Segment Anything (SAM) — ~2.5 GB
mkdir -p loop/ComfyUI/models/sam
wget -q -O loop/ComfyUI/models/sam/sam_vit_h_4b8939.pth \
  "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"

# Grounding DINO — ~660 MB
mkdir -p loop/ComfyUI/models/grounding-dino
wget -q -O loop/ComfyUI/models/grounding-dino/groundingdino_swint_ogc.pth \
  "https://github.com/IDEA-Research/GroundingDINO/releases/download/v0.1.0-alpha/groundingdino_swint_ogc.pth"
```

---

## 8. Configuration

Copy the default template and open the interactive editor:

```bash
cd $SOXHLET_ROOT
cp config.yaml.default config.yaml
python menuconfig.py
```

At minimum, verify these settings in `config.yaml`:

```yaml
compute:
  comfyui:
    backend: auto      # auto | mps | cuda | rocm | cpu
    device_id:         # optional: integer index for eGPU or multi-GPU systems

broker:
  stomp_url:    stomp://admin:admin@localhost:61613

database:
  path: auto           # resolves to {repo_root}/pipeline.db

models:
  vlm:
    filename: Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf

tactical:
  model:
    filename:   Qwen3-72B-abliterated-Q4_K_M.gguf
    server_url: http://localhost:8080/v1
    model_name: qwen3-72b

ui:
  host: 0.0.0.0
  port: 7860
```

The `backend: auto` setting detects the platform at startup: macOS → MPS;
NVIDIA present → CUDA; AMD ROCm present → ROCm; otherwise CPU. Set
`device_id` to an integer device index when multiple GPUs are present (e.g.
an eGPU over Thunderbolt).

Run the environment checker to verify all dependencies are resolvable:

```bash
python check_env.py
```

---

## 9. Build Rust Binaries

```bash
cd $SOXHLET_ROOT/loop/scorers
~/.cargo/bin/cargo build --release
```

This produces four binaries in `target/release/`:

| Binary | Role |
|--------|------|
| `router` | Fans out `loop.events` → `scorer.requests` |
| `aggregator` | Merges scorer results, emits verdicts |
| `coordinator` | XA 2PC + conversation/workflow/budget API (Unix socket) |
| `lancedb_manager` | Writes terminal records to LanceDB |

---

## 10. First Run

```bash
# Terminal 1: Artemis broker
$SOXHLET_ROOT/loop/start_broker.sh

# Terminal 2: Full pipeline (all components, including llama.cpp server and UI)
$SOXHLET_ROOT/loop/start_loop.sh

# Terminal 3: Open the browser UI
open http://localhost:7860
```

`start_loop.sh` starts components in this order:
1. **Parallel**: Artemis broker readiness check, ComfyUI, llama.cpp server
2. Sleep 10 s for slow-starting services
3. Coordinator
4. Sleep 1 s
5. Rust workers (router, aggregator, lancedb_manager), Python scorers
   (clip, artifact, vlm), comfyui_worker, tactical_llm
6. UI server (`server.py`)
7. Monitor

From the browser UI at `http://localhost:7860`:
1. Create a **conversation** (a named project).
2. Create a **workflow** (choose a workflow JSON and parameters).
3. Click **Start** to submit a prompt — images appear in the gallery as they
   are generated and scored.

---

## 11. Verification

### Browser UI

Open `http://localhost:7860`. The gallery section should be visible. Creating
a conversation and workflow exercises the coordinator socket API end-to-end.

### Broker console

Open `http://localhost:8161` (default credentials: admin / admin). Confirm
the following addresses appear under **Addresses**:

- `loop.request` (anycast)
- `loop.events` (multicast)
- `scorer.requests` (multicast)
- `scorer.result` (anycast)
- `tactical.decisions` (multicast)
- `pipeline.dead` (anycast)

### Dead-letter monitor

In a separate terminal, start the dead-letter monitor to catch any messages
that fail processing:

```bash
$SOXHLET_ROOT/loop/scorers/venv/bin/python $SOXHLET_ROOT/loop/monitor.py
```

Any message appearing in the monitor output indicates a processing error; the
log will include the original queue name and message body.

### Tests

```bash
cd $SOXHLET_ROOT
make test      # all Rust + Python tests
```

---

## Stopping the Pipeline

On macOS and Linux:

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
"$SOXHLET_ROOT/loop/artemis-broker/bin/artemis" stop
```

On Windows (Git Bash):

```bash
taskkill /f /fi "IMAGENAME eq python.exe"
taskkill /f /fi "IMAGENAME eq coordinator.exe"
taskkill /f /fi "IMAGENAME eq router.exe"
taskkill /f /fi "IMAGENAME eq aggregator.exe"
taskkill /f /fi "IMAGENAME eq lancedb_manager.exe"
taskkill /f /fi "IMAGENAME eq llama-server.exe"
"$SOXHLET_ROOT/loop/artemis-broker/bin/artemis.cmd" stop
```
