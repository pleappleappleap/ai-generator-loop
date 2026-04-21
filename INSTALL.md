# Installation Guide

Step-by-step setup for the AI image generation pipeline on Apple Silicon Mac.
For architecture and design documentation see `ARCHITECTURE.pdf` (`make doc`).

---

## 1. Hardware Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| Chip | Apple Silicon (M1/M2/M3) | M3 Ultra |
| Unified Memory | 64 GB | 96 GB |
| Free Disk | 100 GB | 200 GB |

The pipeline holds ~44 GB of models in unified memory simultaneously. A 64 GB
machine can run everything except the tactical LLM concurrently; with 96 GB
all components run at full quality settings.

---

## 2. System Software

### macOS and Xcode tools

```bash
xcode-select --install
```

macOS 14 (Sonoma) or later is required for Metal Performance Shaders used by
the ML models.

### Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Python 3.11

```bash
brew install python@3.11
```

### Rust

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

### yq (YAML query tool — used by shell scripts)

```bash
brew install yq
```

### MacTeX (optional — only needed to render ARCHITECTURE.pdf)

```bash
brew install --cask mactex
```

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

The pipeline scripts expect the broker data directory at
`{repo_root}/loop/artemis-broker` by default (overridable in `config.yaml`).

```bash
cd /path/to/ai-image   # repo root
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
<!-- AMQP 1.0 — used by Rust components (router, aggregator, coordinator, lancedb_manager) -->
<acceptor name="amqp">tcp://0.0.0.0:5672?protocols=AMQP</acceptor>

<!-- STOMP — used by Python components (comfyui_worker, scorers, tactical_llm, monitor) -->
<acceptor name="stomp">tcp://0.0.0.0:61613?protocols=STOMP</acceptor>
```

The default Artemis template ships both acceptors commented out. Uncomment or
add them. The AMQP acceptor on port 5672 is standard; the STOMP acceptor on
61613 is the Artemis STOMP default.

---

## 4. Clone the Repository

```bash
git clone <repo-url> ~/ai-image
cd ~/ai-image
```

All subsequent paths assume `~/ai-image` as the repo root. The `config.yaml`
`auto` path resolution is relative to the location of `config.yaml` itself,
so moving the repo requires updating `config.yaml`.

---

## 5. Python Environments

There are two separate virtual environments: one at the repo root (used by
`session.py`, `tactical_llm.py`, and utilities) and one inside `loop/scorers`
(used by the three scorer processes, which share ML libraries).

### Root environment

```bash
cd ~/ai-image
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install lancedb open_clip_torch torch stomp.py pydantic rich
deactivate
```

### Scorers environment

```bash
cd ~/ai-image/loop/scorers
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Core dependencies
pip install stomp.py open_clip_torch torch torchvision

# Artifact detector
pip install transformers accelerate pillow

# VLM scorer (llama.cpp Python bindings with Metal support)
CMAKE_ARGS="-DLLAMA_METAL=on" pip install llama-cpp-python --no-binary llama-cpp-python

# Tactical LLM
pip install llama-cpp-python  # already installed above

# Test dependencies
pip install pytest pytest-mock
deactivate
```

To activate the scorers environment in a shell, use the provided script:

```bash
source ~/ai-image/loop/scorers/activate.sh
```

---

## 6. Model Downloads

All models are stored under `loop/scorers/models/`. The tactical LLM lives
in a subdirectory of its own.

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
cd ~/ai-image/loop/scorers
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

Download the GGUF quantised model:

```bash
mkdir -p ~/ai-image/loop/scorers/models/vlm
# ~5 GB download
curl -L -o ~/ai-image/loop/scorers/models/vlm/Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf \
  "https://huggingface.co/bartowski/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/Qwen2.5-VL-7B-Instruct-Q5_K_M.gguf"
```

### Tactical LLM (Qwen3-72B)

Qwen3-72B is a hybrid thinking model: it reasons step-by-step through
ambiguous decisions and responds directly for clear-cut cases, controlled
by a `<think>` token in the prompt.

```bash
mkdir -p ~/ai-image/loop/scorers/models/tactical
# ~43 GB download
curl -L -o ~/ai-image/loop/scorers/models/tactical/Qwen3-72B-abliterated-Q4_K_M.gguf \
  "https://huggingface.co/bartowski/Qwen3-72B-abliterated-GGUF/resolve/main/Qwen3-72B-abliterated-Q4_K_M.gguf"
```

Verify the filenames match `config.yaml.default` → `models.vlm.filename` and
`tactical.model.filename`, or update `config.yaml` to match what you
downloaded.

---

## 7. ComfyUI

`make prereqs` clones ComfyUI into `loop/ComfyUI/` and installs its
dependencies into a dedicated venv at `loop/ComfyUI/venv/`. If you need
to run this step manually:

```bash
git clone --depth=1 https://github.com/comfyanonymous/ComfyUI /tmp/_comfyui_clone
rsync -a --ignore-existing /tmp/_comfyui_clone/ ~/ai-image/loop/ComfyUI/
rm -rf /tmp/_comfyui_clone
python3.11 -m venv ~/ai-image/loop/ComfyUI/venv
~/ai-image/loop/ComfyUI/venv/bin/pip install -r ~/ai-image/loop/ComfyUI/requirements.txt
```

### Custom nodes

The inpainting workflow requires ComfyUI-Manager and the segment-anything
custom node. Inside the ComfyUI browser UI:

1. Start ComfyUI: `~/ai-image/loop/ComfyUI/launch.sh`
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
cd ~/ai-image
cp config.yaml.default config.yaml
python menuconfig.py
```

At minimum, verify these settings in `config.yaml`:

```yaml
broker:
  stomp_url:    stomp://admin:admin@localhost:61613
  artemis_data: /path/to/ai-image/loop/artemis-broker   # or leave auto

database:
  path: auto      # resolves to {repo_root}/pipeline.db

tactical:
  model:
    filename: Qwen2.5-14B-Instruct-Q5_K_M.gguf   # must match downloaded file
```

Run the environment checker to verify all dependencies are resolvable:

```bash
python check_env.py
```

---

## 9. Build Rust Binaries

```bash
cd ~/ai-image/loop/scorers
cargo build --release
```

This produces four binaries in `target/release/`:

| Binary | Role |
|--------|------|
| `router` | Fans out `loop.events` → `scorer.requests` |
| `aggregator` | Merges scorer results, emits verdicts |
| `coordinator` | XA 2PC + Python budget API (Unix socket) |
| `lancedb_manager` | Writes terminal records to LanceDB |

---

## 10. First Run

Start each component in order. The broker must be running before any pipeline
process starts.

```bash
# Terminal 1 — Artemis broker
~/ai-image/loop/start_broker.sh

# Terminal 2 — Full pipeline (all Python and Rust components)
~/ai-image/loop/start_loop.sh

# Terminal 3 — Submit a session
cd ~/ai-image
source venv/bin/activate
python tactical-llm/session.py \
  --prompt "two people walking in a park, photorealistic, golden hour" \
  --max-retries 3 \
  --monitor
```

The `--monitor` flag polls the coordinator's budget state and prints live
progress until the session resolves (accepted, give_up, or budget exhausted).

---

## 11. Verification

### Broker console

Open `http://localhost:8161` (default credentials: admin / admin). Confirm
the following addresses appear under **Addresses**:

- `loop.request` (anycast)
- `loop.events` (multicast)
- `scorer.requests` (multicast)
- `scorer.result` (anycast)
- `pipeline.dead` (anycast)

### Dead-letter monitor

In a separate terminal, start the dead-letter monitor to catch any messages
that fail processing:

```bash
source ~/ai-image/venv/bin/activate
python ~/ai-image/loop/monitor.py
```

Any message appearing in the monitor output indicates a processing error; the
log will include the original queue name and message body.

### Rust tests

```bash
cd ~/ai-image/loop/scorers
cargo test -p coordinator
```

### Python tests

```bash
cd ~/ai-image/tactical-llm
source ../venv/bin/activate
python -m pytest tests/ -v
```

---

## Stopping the Pipeline

```bash
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
"$ARTEMIS_DATA/bin/artemis" stop
```

Or use `loop/start_loop.sh`'s companion stop script if one exists.
