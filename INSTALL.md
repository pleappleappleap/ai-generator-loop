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

1. **Git Bash**  -  install from https://git-scm.com/downloads. This provides
   bash, git, make, curl, and wget in one package. Run all `make` commands
   from the Git Bash terminal, not CMD or PowerShell.

2. **winget**  -  ships with Windows 11. On Windows 10, install the
   [App Installer](https://apps.microsoft.com/detail/9NBLGGH4NNS1) from the
   Microsoft Store. `make prereqs-system` uses winget to install Docker.

### All platforms  -  clone the repository

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
| wget |  -  | Yes | macOS uses curl (system native) |
| Python 3.11 | Yes | Yes | via Homebrew / distro package manager |
| yq | Yes | Yes | YAML query tool used by shell scripts |
| Docker / K3s | Yes | Yes | K3s hosts Artemis and PostgreSQL |
| kubectl | Yes | Yes | for port-forwards managed by `middleware.sh` |
| Java 21+ | Yes | Yes | for the Spring Boot pipeline |
| Rust + cargo | Yes | Yes | via rustup |
| sqlx-cli | Yes | Yes | Rust SQLite compile-time query verification |
| protoc | Yes | Yes | required by LanceDB Rust crate |

TeX is **not** installed automatically. It is only needed to regenerate
`ARCHITECTURE.pdf`:

| Platform | Command |
|----------|---------|
| macOS | `brew install --cask mactex` |
| Linux | `sudo apt install texlive-full latexmk` (or equivalent) |
| Windows | Install MiKTeX from https://miktex.org/download |

---

## 4. Middleware (Artemis + PostgreSQL via K3s)

Artemis and PostgreSQL run as K3s workloads. `middleware.sh` manages them
and establishes `kubectl port-forward` tunnels so local processes can reach
them on the 12000 range.

### Install K3s

```bash
# macOS  -  via Homebrew (uses Rancher Desktop or k3d)
# Linux
curl -sfL https://get.k3s.io | sh -

# Verify
kubectl get nodes
```

### Apply the middleware manifests

```bash
kubectl apply -f middleware/k8s/
```

### Start middleware (and port-forwards)

```bash
~/soxhlet/middleware.sh start
```

This starts or resumes the K3s workloads and establishes:

| Service | Local port | K3s target |
|---------|-----------|------------|
| PostgreSQL | 12007 | `svc/postgres:5432` |
| Artemis AMQP | 12008 | `svc/artemis:61616` |
| Artemis console | 12009 | `svc/artemis:8161` |

### Management console

`http://localhost:12009`  -  credentials: **admin / admin**

---

## 5. Python Environments

There are **three** separate virtual environments:

- **Root venv** (`venv/`): used by utilities such as `check_env.py` and `model_picker.py`.
- **Scorers venv** (`loop/scorers/venv/`): used by all scorer processes.
  Also provides `mlx_lm` for the tactical and strategic LLM servers.
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

```bash
cd $SOXHLET_ROOT/loop/scorers
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

The scorers `requirements.txt` includes `mlx-lm` (for the tactical and
strategic LLM servers), `openai`, and `stomp.py`.

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
filename. Update `config.yaml` -> `models.checkpoint` if you use a
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

### Tactical LLM (Qwen3-Next-80B-A3B, MLX 4-bit)

The tactical LLM is an ultra-sparse MoE model: 80B total parameters, ~3B active
per forward pass. It runs as an **mlx\_lm.server** (started automatically by
`loop.sh`) and exposes an OpenAI-compatible API on port 12001.

```bash
# ~45 GB download (pre-converted MLX 4-bit)
source $SOXHLET_ROOT/loop/scorers/venv/bin/activate
huggingface-cli download \
  huihui-ai/Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-4bit \
  --local-dir $SOXHLET_ROOT/loop/scorers/models/tactical/Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-4bit
```

### Strategic LLM (Qwen3-Next-80B-A3B-Thinking, bf16)

Same base architecture as tactical. Full bf16 precision; `mlx_lm.server`
streams expert arrays from SSD. Extended chain-of-thought for strategic review.

```bash
# ~160 GB download (bf16 safetensors)
source $SOXHLET_ROOT/loop/scorers/venv/bin/activate
huggingface-cli download \
  huihui-ai/Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated \
  --local-dir $SOXHLET_ROOT/loop/scorers/models/strategic/Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated
```

Model names and paths are configured in `config.yaml` under `tactical.model`
and `strategic.model`.

### Automated download

`make models` downloads all models using paths from `config.yaml`:

```bash
make models
```

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
# Segment Anything (SAM)  -  ~2.5 GB
mkdir -p loop/ComfyUI/models/sam
wget -q -O loop/ComfyUI/models/sam/sam_vit_h_4b8939.pth \
  "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"

# Grounding DINO  -  ~660 MB
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
    port: 12006
    device_id:         # optional: integer index for eGPU or multi-GPU systems

broker:
  url: tcp://localhost:12008   # Artemis via port-forward from middleware.sh

tactical:
  model:
    name: Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-4bit
    server_url: http://localhost:12001/v1

strategic:
  model:
    name: Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated
    server_url: http://localhost:12005/v1
```

The `backend: auto` setting detects the platform at startup: macOS -> MPS;
NVIDIA present -> CUDA; AMD ROCm present -> ROCm; otherwise CPU. Set
`device_id` to an integer device index when multiple GPUs are present (e.g.
an eGPU over Thunderbolt).

Run the environment checker to verify all dependencies are resolvable:

```bash
python check_env.py
```

---

## 9. Build Binaries

### Rust

```bash
cd $SOXHLET_ROOT/loop/scorers
~/.cargo/bin/cargo build --release
```

This produces four binaries in `target/release/`:

| Binary | Role |
|--------|------|
| `router` | Fans out `loop.events` -> `scorer.requests` |
| `aggregator` | Merges scorer results, emits verdicts |
| `coordinator` | XA 2PC + conversation/workflow/budget API (Unix socket) |
| `lancedb_manager` | Writes terminal records to LanceDB |

### Java pipeline

```bash
cd $SOXHLET_ROOT/pipeline
mvn package -DskipTests
```

---

## 10. First Run

```bash
# Start middleware (Artemis + PostgreSQL via K3s; port-forwards 12007/12008/12009)
$SOXHLET_ROOT/middleware.sh start

# Start the Loop super-component
$SOXHLET_ROOT/loop.sh start

# Open the browser UI
open http://localhost:12000
```

`loop.sh start` waits for each tier before starting the next:
1. Middleware (Artemis on 12008, PostgreSQL on 12007) must be healthy
2. **Parallel**: ComfyUI (12006) and tactical LLM server (mlx_lm.server on 12001)
3. Scorers: clip (12002), artifact (12003), vlm (12004)
4. Spring Boot pipeline (12000)

From the browser UI at `http://localhost:12000`:
1. Create a **conversation** (a named project).
2. Create a **workflow** (choose a workflow JSON and parameters).
3. Click **Start** to submit a prompt  -  images appear in the gallery as they
   are generated and scored.

---

## 11. Verification

### Browser UI

Open `http://localhost:12000`. The gallery section should be visible. Creating
a conversation and workflow exercises the coordinator socket API end-to-end.

### Broker console

Open `http://localhost:12009` (default credentials: admin / admin). Confirm
the following addresses appear under **Addresses**:

- `loop.request` (anycast)
- `loop.events` (multicast)
- `scorer.requests` (multicast)
- `scorer.result` (anycast)
- `tactical.decisions` (multicast)
- `pipeline.dead` (anycast)

### Dead-letter queue

Failed messages land in the `pipeline.dead` queue. Inspect them via the
Artemis management console at `http://localhost:12009` under **Queues ->
pipeline.dead**.

### Tests

```bash
cd $SOXHLET_ROOT
make test      # all Rust + Python tests
```

---

## Stopping the Pipeline

```bash
# Stop the Loop super-component
$SOXHLET_ROOT/loop.sh stop

# Stop middleware (and port-forwards)
$SOXHLET_ROOT/middleware.sh stop
```
