# Installation Guide

For architecture and design documentation see `ARCHITECTURE.pdf` (`make docs`).

---

## 1. Hardware Requirements

| Platform | Requirement | Minimum | Recommended |
|----------|-------------|---------|-------------|
| Apple Silicon (MPS) | Unified memory | 64 GB | 96 GB |
| NVIDIA (CUDA) | GPU VRAM | 24 GB | 48 GB+ |
| NVIDIA (CUDA) | System RAM | 32 GB | 64 GB |
| AMD (ROCm) | GPU VRAM | 24 GB | 48 GB+ |
| AMD (ROCm) | System RAM | 32 GB | 64 GB |
| CPU only | System RAM | 64 GB | 128 GB+ |
| All platforms | Free disk | 100 GB | 200 GB |

CPU-only mode is supported but very slow. On discrete-GPU systems with
insufficient VRAM, the tactical LLM can be partially offloaded to CPU via
`tactical.model.n_gpu_layers` in `config.yaml`.

---

## 2. Bootstrap Prerequisites

These must be in place **before** you can run `make`. Everything else is
handled automatically by `make setup`.

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

The pipeline scripts detect `SOXHLET_ROOT` automatically from their own
location. Adding it to your shell profile is optional but convenient when
running scripts from other directories:

```bash
export SOXHLET_ROOT=~/soxhlet
```

---

## 3. Setup

With the bootstrap in place, a single command handles everything:

```bash
make setup
```

This runs in order: system prerequisites, Python environments, default
configuration, model downloads (~220 GB total), ComfyUI and custom nodes,
K3s middleware manifests, and the Java pipeline build. It is safe to re-run.

### What `make setup` installs

| Step | What it does |
|------|-------------|
| `make prereqs-system` | Python 3.11, yq, Java 21, HuggingFace CLI, K3s / Colima |
| `make prereqs-python` | Root venv, scorers venv, ComfyUI venv, tactical-llm venv |
| `make config-default` | Creates `config.yaml` from defaults |
| `make models` | Artifact detector, VLM scorer, tactical LLM (~45 GB), strategic LLM (~160 GB) |
| `make comfyui-nodes` | ComfyUI clone + venv, ComfyUI-Manager, inpaint and SAM custom nodes |
| `make middleware-apply` | Applies K3s manifests for Artemis and PostgreSQL |
| `make build` | Ivy dependency resolution and `javac --release 21` |

### TeX (optional)

TeX is not installed by `make setup`. It is only needed to regenerate
`ARCHITECTURE.pdf` via `make docs`:

| Platform | Command |
|----------|---------|
| macOS | `brew install --cask mactex` |
| Linux | `sudo apt install texlive-full latexmk` (or equivalent) |
| Windows | Install MiKTeX from https://miktex.org/download |

### Image generation checkpoint (ComfyUI)

`make setup` does not download a generation checkpoint — there are too many
options. Download your preferred checkpoint (SD 1.5, SDXL, Flux, or any
ComfyUI-compatible model) and place it in:

```
loop/ComfyUI/models/checkpoints/
```

The workflow JSON files under `loop/workflows/` reference the checkpoint by
filename. Update `config.yaml` → `models.checkpoint` if you use a
non-default filename.

### Optional inpainting models

The SAM and Grounding DINO weights are not downloaded automatically (large
files, inpainting-only). Download them if you intend to use inpainting:

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

## 4. Configuration

`config.yaml` is the single source of truth for all components — shell
scripts, Python scorers, and the Java pipeline. You do not need to edit
`pipeline/src/main/resources/application.yml`; `loop.sh` reads `config.yaml`
and exports the corresponding Spring Boot environment variables before
starting the Java pipeline.

`make setup` creates `config.yaml` from defaults. To customise values
interactively (requires `yq`):

```bash
make menuconfig
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

database:
  postgres_url: jdbc:postgresql://localhost:12007/pipeline
  username: pipeline
  password: pipeline

thresholds:
  clip: 0.25           # minimum CLIP score to pass the first gate
  artifact: 0.50       # minimum artifact-detector confidence to pass the second gate

tactical:
  model:
    name: Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-4bit
    server_url: http://localhost:12001/v1

strategic:
  model:
    name: Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated
    server_url: http://localhost:12005/v1
```

The `backend: auto` setting detects the platform at startup: macOS → MPS;
NVIDIA present → CUDA; AMD ROCm present → ROCm; otherwise CPU. Set
`device_id` to an integer device index when multiple GPUs are present.

Run the environment checker to verify all dependencies are resolvable:

```bash
python check_env.py
```

---

## 5. First Run

```bash
./loop.sh start
```

`loop.sh start` starts middleware first, then all Loop components in
dependency order, blocking until each tier is healthy:

1. Middleware (Artemis on 12008, PostgreSQL on 12007)
2. **Parallel**: ComfyUI (12006) and tactical LLM server (mlx_lm.server on 12001)
3. Scorers: clip (12002), artifact (12003), vlm (12004)
4. Spring Boot pipeline (12000)

Open the browser UI at `http://localhost:12000`.

From the browser UI:
1. Create a **conversation** (a named project).
2. Create a **workflow** (choose a workflow JSON and parameters).
3. Click **Start** to submit a prompt  -  images appear in the gallery as they
   are generated and scored.

### Autonomous escalation handoff

To enable autonomous mode switching — where a tactical `escalate` decision
automatically hands off to the strategic session without operator intervention:

```bash
./loop.sh start --auto-escalate
```

Without `--auto-escalate`, the pipeline shuts down on escalation and the
operator starts the strategic session manually:

```bash
./strategic.sh start
```

The flag is per-invocation and not persisted, which prevents runaway
mode-switch cycles if the pipeline exits unexpectedly.

---

## 6. Verification

### Browser UI

Open `http://localhost:12000`. The gallery section should be visible. Creating
a conversation and workflow exercises the coordinator socket API end-to-end.

### Component health

```bash
./loop.sh health
```

### Broker console

Open `http://localhost:12009` (credentials: **admin / admin**). Confirm the
following addresses appear under **Addresses**:

- `loop.request` (anycast)
- `loop.events` (multicast)
- `scorer.requests` (multicast)
- `scorer.result` (anycast)
- `tactical.decisions` (multicast)
- `pipeline.dead` (anycast)

### Dead-letter queue

Failed messages land in the `pipeline.dead` queue. Inspect them via the
Artemis console at `http://localhost:12009` under **Queues → pipeline.dead**.

### Tests

```bash
make test
```

---

## Stopping the Pipeline

```bash
./loop.sh stop        # stop Loop; middleware keeps running
./loop.sh stop --all  # stop Loop and middleware
```
