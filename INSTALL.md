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

## 2. System Software

A POSIX-compatible shell is required on all platforms. On Windows, use
Git Bash or WSL.

### Python 3.11

| Platform | Command |
|----------|---------|
| macOS (Homebrew) | `brew install python@3.11` |
| Debian / Ubuntu | `sudo apt install python3.11 python3.11-venv` |
| Fedora / RHEL | `sudo dnf install python3.11` |
| Windows (winget) | `winget install Python.Python.3.11` |

### Rust

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
. ~/.cargo/env
```

### yq (YAML query tool, used by shell scripts)

| Platform | Command |
|----------|---------|
| macOS (Homebrew) | `brew install yq` |
| Linux (snap) | `snap install yq` |
| Linux (manual) | Download from https://github.com/mikefarah/yq/releases |
| Windows (Chocolatey) | `choco install yq` |
| Windows (Scoop) | `scoop install yq` |

### TeX distribution (optional, only needed to render ARCHITECTURE.pdf)

| Platform | Command |
|----------|---------|
| macOS | `brew install --cask mactex` |
| Linux | `sudo apt install texlive-full` (or equivalent) |
| Windows | Install MiKTeX from https://miktex.org/download |

---

## 3. ActiveMQ Artemis

The pipeline requires Artemis 2.31 or later with both STOMP and AMQP 1.0
acceptors enabled.

### Download and extract

```bash
# Download from https://activemq.apache.org/components/artemis/download/
# Replace X.Y.Z with the actual version
curl -LO https://downloads.apache.org/activemq/activemq-artemis/X.Y.Z/apache-artemis-X.Y.Z-bin.tar.gz
tar -xzf apache-artemis-X.Y.Z-bin.tar.gz
sudo mv apache-artemis-X.Y.Z /opt/artemis
```

### Create the broker instance

The pipeline scripts resolve the broker data directory from `config.yaml`
(key `broker.artemis_data`; default: `auto`, resolving to
`{repo_root}/loop/artemis-broker`).

```bash
cd $AI_IMAGE_ROOT
/opt/artemis/bin/artemis create loop/artemis-broker \
  --user admin \
  --password admin \
  --require-login \
  --allow-anonymous false
```

### Configure acceptors

Edit `loop/artemis-broker/etc/broker.xml`. Ensure the `<acceptors>` block
contains both of these entries:

```xml
<!-- AMQP 1.0: used by Rust components (router, aggregator, coordinator, lancedb_manager) -->
<acceptor name="amqp">tcp://0.0.0.0:5672?protocols=AMQP</acceptor>

<!-- STOMP: used by Python components (comfyui_worker, scorers, tactical_llm, server, monitor) -->
<acceptor name="stomp">tcp://0.0.0.0:61613?protocols=STOMP</acceptor>
```

The default Artemis template ships both acceptors commented out. Uncomment or
add them. The AMQP acceptor on port 5672 is standard; the STOMP acceptor on
61613 is the Artemis STOMP default.

### Expose the management console on the network

The Artemis web console binds to `localhost` by default. To make it reachable
from other machines, edit `loop/artemis-broker/etc/bootstrap.xml` and change
the `<web>` binding:

```xml
<web path="web" rootRedirectLocation="index.html">
    <binding uri="http://0.0.0.0:8161" apps="console,metrics"/>
</web>
```

The default value is `http://localhost:8161`; replace `localhost` with
`0.0.0.0` to listen on all interfaces. Restart the broker for the change to
take effect.

---

## 4. Clone the Repository

```bash
git clone <repo-url> ~/ai-image
cd ~/ai-image
```

Export `AI_IMAGE_ROOT` so the pipeline scripts can locate `config.yaml` from
any working directory. Add this to your shell profile (`.bashrc`, `.profile`,
or equivalent):

```bash
export AI_IMAGE_ROOT=~/ai-image
```

The pipeline shell scripts determine the repository root using the first of
the following that is set: (1) a path passed as the first command-line
argument, (2) the `AI_IMAGE_ROOT` environment variable, or (3) the current
working directory if it contains `config.yaml` and a `loop/` subdirectory.

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
cd $AI_IMAGE_ROOT
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
cd $AI_IMAGE_ROOT/loop/scorers
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
. $AI_IMAGE_ROOT/loop/scorers/activate.sh
```

---

## 6. Model Downloads

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
cd $AI_IMAGE_ROOT/loop/scorers
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
mkdir -p $AI_IMAGE_ROOT/loop/scorers/models/vlm
# ~5 GB download
curl -L -o $AI_IMAGE_ROOT/loop/scorers/models/vlm/Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf \
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
mkdir -p $AI_IMAGE_ROOT/loop/scorers/models/tactical
# ~43 GB download
curl -L -o $AI_IMAGE_ROOT/loop/scorers/models/tactical/Qwen3-72B-abliterated-Q4_K_M.gguf \
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

`make prereqs` clones ComfyUI into `loop/ComfyUI/` and installs its
dependencies into a dedicated venv at `loop/ComfyUI/venv/`. To run this
step manually:

```bash
git clone --depth=1 https://github.com/comfyanonymous/ComfyUI /tmp/_comfyui_clone
cp -rn /tmp/_comfyui_clone/. $AI_IMAGE_ROOT/loop/ComfyUI/
rm -rf /tmp/_comfyui_clone
python3.11 -m venv $AI_IMAGE_ROOT/loop/ComfyUI/venv
$AI_IMAGE_ROOT/loop/ComfyUI/venv/bin/pip install -r $AI_IMAGE_ROOT/loop/ComfyUI/requirements.txt
```

### Custom nodes

The inpainting workflow requires ComfyUI-Manager and the segment-anything
custom node. Inside the ComfyUI browser UI:

1. Start ComfyUI: `$AI_IMAGE_ROOT/loop/ComfyUI/launch.sh`
2. Open `http://127.0.0.1:8188`
3. Install **ComfyUI-Manager** from the Manager menu
4. Use Manager to install: `ComfyUI-Inpaint-Nodes`, `comfyui_segment_anything`

### Optional vision models (for inpainting)

```bash
# Segment Anything (SAM)
mkdir -p loop/ComfyUI/models/sam
curl -L -o loop/ComfyUI/models/sam/sam_vit_h_4b8939.pth \
  "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"

# Grounding DINO
mkdir -p loop/ComfyUI/models/grounding-dino
curl -L -o loop/ComfyUI/models/grounding-dino/groundingdino_swint_ogc.pth \
  "https://github.com/IDEA-Research/GroundingDINO/releases/download/v0.1.0-alpha/groundingdino_swint_ogc.pth"
```

---

## 8. Configuration

Copy the default template and open the interactive editor:

```bash
cd $AI_IMAGE_ROOT
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
  artemis_data: auto   # resolves to {repo_root}/loop/artemis-broker

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
cd $AI_IMAGE_ROOT/loop/scorers
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
$AI_IMAGE_ROOT/loop/start_broker.sh

# Terminal 2: Full pipeline (all components, including llama.cpp server and UI)
$AI_IMAGE_ROOT/loop/start_loop.sh

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
$AI_IMAGE_ROOT/loop/scorers/venv/bin/python $AI_IMAGE_ROOT/loop/monitor.py
```

Any message appearing in the monitor output indicates a processing error; the
log will include the original queue name and message body.

### Tests

```bash
cd $AI_IMAGE_ROOT
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
"$AI_IMAGE_ROOT/loop/artemis-broker/bin/artemis" stop
```

On Windows (Git Bash):

```bash
taskkill /f /fi "IMAGENAME eq python.exe"
taskkill /f /fi "IMAGENAME eq coordinator.exe"
taskkill /f /fi "IMAGENAME eq router.exe"
taskkill /f /fi "IMAGENAME eq aggregator.exe"
taskkill /f /fi "IMAGENAME eq lancedb_manager.exe"
taskkill /f /fi "IMAGENAME eq llama-server.exe"
"$AI_IMAGE_ROOT/loop/artemis-broker/bin/artemis.cmd" stop
```
