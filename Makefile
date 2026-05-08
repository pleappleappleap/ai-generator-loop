# Git Bash on Windows sets MSYSTEM (e.g. MINGW64); CMD/PowerShell does not.
# Only block CMD/PowerShell — Git Bash users can proceed normally.
ifeq ($(OS),Windows_NT)
  ifndef MSYSTEM
    $(error Windows CMD/PowerShell detected. Run from Git Bash instead: https://git-scm.com/downloads)
  endif
endif

.PHONY: all lint typecheck test build format docs clean distclean config check \
        prereqs prereqs-system prereqs-python models models-pick sqlx-prepare setup \
        artemis-broker artemis-pull docker-install comfyui-nodes

# Use generated config.yaml if present, otherwise fall back to the template.
CFG := $(or $(wildcard config.yaml),config.yaml.default)

ARTEMIS_IMAGE    := apache/activemq-artemis:latest
ARTEMIS_PULL_OK  := loop/.artemis-image-pulled
DOCKER_OK        := loop/.docker-installed
COMFYUI_NODES_OK := loop/ComfyUI/custom_nodes/.nodes-installed

all: venv/.installed loop/scorers/venv/.installed $(COMFYUI_NODES_OK) \
     models $(ARTEMIS_PULL_OK) lint typecheck test build

lint:
	$(MAKE) -C loop lint
	$(MAKE) -C tactical-llm lint

typecheck:
	$(MAKE) -C loop typecheck
	$(MAKE) -C tactical-llm typecheck

test:
	$(MAKE) -C loop test
	$(MAKE) -C tactical-llm test

build:
	$(MAKE) -C loop build

format:
	$(MAKE) -C loop format

docs:
	latexmk -pdf -interaction=nonstopmode ARCHITECTURE.tex
	$(MAKE) -C loop docs
	$(MAKE) -C tactical-llm docs

sqlx-prepare:
	$(MAKE) -C loop sqlx-prepare

clean:
	latexmk -C ARCHITECTURE.tex
	rm -f *.aux *.log *.out *.toc *.fls *.fdb_latexmk
	rm -rf __pycache__
	$(MAKE) -C loop clean
	$(MAKE) -C tactical-llm clean

# Auto-downloaded scorer model directories are declared in .gitignore.
# distclean reads that file directly — no git command required — so adding a
# new model directory to .gitignore is sufficient to make distclean handle it.
# Within each such directory a .gitkeep sentinel (if present) is preserved so
# the placeholder survives and make models knows where to put files.
SCORER_MODEL_DIRS := $(shell grep -E '^loop/scorers/models/' .gitignore | sed 's|/*$$||')

# Remove everything that can be regenerated: venvs, the ComfyUI clone, and
# auto-downloaded model files.  Reads .gitignore and .gitkeep files as the
# source of truth — does not require the git CLI or a .git directory.
# The only hardcoded exclusions protect user-placed data that cannot be
# re-downloaded (SDXL checkpoints, custom workflows).
# After distclean, restore with: make setup
distclean: clean
	@echo "==> Removing Python virtual environments..."
	rm -rf venv loop/scorers/venv
	@echo "==> Removing auto-downloaded scorer models (preserving .gitkeep sentinels)..."
	@for d in $(SCORER_MODEL_DIRS); do \
	  if [ -d "$$d" ]; then \
	    echo "    cleaning $$d"; \
	    keep=0; [ -f "$$d/.gitkeep" ] && keep=1; \
	    rm -rf "$$d"; \
	    mkdir -p "$$d"; \
	    [ "$$keep" = "1" ] && touch "$$d/.gitkeep"; \
	  fi; \
	done
	@echo "==> Removing ComfyUI clone (models/ and workflows/ preserved)..."
	find loop/ComfyUI -mindepth 1 -maxdepth 1 \
	  ! -name models ! -name workflows ! -name launch.sh ! -name README.md \
	  -exec rm -rf {} +
	@echo "==> distclean complete. Run 'make setup' to rebuild from scratch."
	@echo "    NOTE: SDXL checkpoints in loop/ComfyUI/models/ were preserved."

# Interactive configuration TUI (menuconfig-style).
# Generates config.yaml from auto-detected defaults.
config:
	@command -v yq >/dev/null 2>&1 || { \
		echo "yq not found. Install it:"; \
		echo "  macOS:  brew install yq"; \
		echo "  Linux:  snap install yq  (or see https://github.com/mikefarah/yq/releases)"; \
		exit 1; \
	}
	python3.11 menuconfig.py

# Validate the environment against the current config.yaml.
# Checks services, GPU backend, model files, venvs, and Rust binaries.
check: config.yaml venv/.installed
	venv/bin/python3 check_env.py

config.yaml:
	@echo "config.yaml not found — running make config first"
	$(MAKE) config

# ── Prerequisites ─────────────────────────────────────────────────────────────

# ── Docker install ─────────────────────────────────────────────────────────────

# Install Docker if not present. Sentinel prevents re-checks on subsequent runs.
# macOS:   colima + docker CLI via Homebrew (no sudo, no GUI required).
# Linux:   Official get.docker.com install script; enables the systemd service.
# Windows: Docker Desktop via winget (Git Bash only — CMD/PowerShell blocked above).
docker-install: $(DOCKER_OK)

$(DOCKER_OK):
	@_docker_wait() { \
	  echo "==> Waiting for Docker daemon (up to 120 s)..."; \
	  n=0; \
	  while ! $${DOCKER_CMD:-docker} info >/dev/null 2>&1; do \
	    n=$$((n+1)); \
	    if [ $$n -ge 60 ]; then \
	      echo "ERROR: Docker daemon did not start within 120 s." >&2; \
	      echo "       Start Docker manually and re-run make." >&2; \
	      exit 1; \
	    fi; \
	    sleep 2; \
	  done; \
	  echo "==> Docker daemon ready."; \
	}; \
	if docker info >/dev/null 2>&1; then \
	  echo "==> Docker already running: $$(docker --version)"; \
	elif command -v brew >/dev/null 2>&1; then \
	  echo "==> Installing colima + docker CLI via Homebrew..."; \
	  brew install colima docker; \
	  echo "==> Starting colima (Docker daemon)..."; \
	  colima start; \
	  _docker_wait; \
	elif [ "$$(uname -s)" = "Linux" ]; then \
	  echo "==> Installing Docker Engine via get.docker.com..."; \
	  wget -qO- https://get.docker.com | sh; \
	  sudo systemctl enable --now docker; \
	  sudo usermod -aG docker "$$(id -un)"; \
	  DOCKER_CMD="sudo docker" _docker_wait; \
	  echo "NOTE: Log out and back in (or run 'newgrp docker') to use docker without sudo."; \
	elif command -v winget >/dev/null 2>&1; then \
	  echo "==> Installing Docker Desktop via winget..."; \
	  winget install --id Docker.DockerDesktop -e \
	    --accept-package-agreements --accept-source-agreements; \
	  echo "==> Launching Docker Desktop..."; \
	  "/c/Program Files/Docker/Docker/Docker Desktop.exe" & \
	  _docker_wait; \
	else \
	  echo "ERROR: Cannot auto-install Docker on this platform." >&2; \
	  echo "       Install from: https://docs.docker.com/get-docker/" >&2; \
	  exit 1; \
	fi
	@mkdir -p loop
	@touch $(DOCKER_OK)

# ── Artemis broker setup (Docker) ─────────────────────────────────────────────

# Pull the official Artemis Docker image once; sentinel file prevents re-pulls.
# The broker runs via start_broker.sh which handles container create/start.
# AMQP (5672), STOMP (61613), and the management console (8161) are pre-enabled
# in the apache/activemq-artemis image — no broker.xml patching needed.
artemis-broker: $(ARTEMIS_PULL_OK)
artemis-pull:   $(ARTEMIS_PULL_OK)

$(ARTEMIS_PULL_OK): $(DOCKER_OK)
	docker pull $(ARTEMIS_IMAGE)
	@mkdir -p loop
	@touch $(ARTEMIS_PULL_OK)
	@echo "==> Artemis image ready. Start the broker with: loop/start_broker.sh"

# Full first-time setup: system deps → Python venvs → config → models → build.
setup: prereqs-system prereqs-python config models build

prereqs: prereqs-system prereqs-python

# Install system-level packages using the detected package manager.
# Installs: wget (Linux), Python 3.11, yq, Docker, Rust (via rustup), protoc.
# LaTeX is optional and only needed for `make doc`.
prereqs-system: docker-install
	@PKG=""; \
	if   command -v brew    >/dev/null 2>&1; then PKG=homebrew; \
	elif command -v apt-get >/dev/null 2>&1; then PKG=apt; \
	elif command -v dnf     >/dev/null 2>&1; then PKG=dnf; \
	elif command -v pacman  >/dev/null 2>&1; then PKG=pacman; \
	elif command -v zypper  >/dev/null 2>&1; then PKG=zypper; \
	else echo "ERROR: no supported package manager found (brew/apt/dnf/pacman/zypper)" >&2; exit 1; fi; \
	echo "==> Package manager: $$PKG"; \
	\
	echo "==> Python 3.11"; \
	if command -v python3.11 >/dev/null 2>&1; then \
	  echo "    already installed: $$(python3.11 --version)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install python@3.11 ;; \
	    apt)      sudo apt-get install -y python3.11 python3.11-venv python3-pip ;; \
	    dnf)      sudo dnf install -y python3.11 ;; \
	    pacman)   sudo pacman -S --noconfirm python ;; \
	    zypper)   sudo zypper install -y python311 ;; \
	  esac; \
	fi; \
	\
	echo "==> wget (Linux download tool)"; \
	if [ "$$PKG" != "homebrew" ] && ! command -v wget >/dev/null 2>&1; then \
	  case "$$PKG" in \
	    apt)    sudo apt-get install -y wget ;; \
	    dnf)    sudo dnf install -y wget ;; \
	    pacman) sudo pacman -S --noconfirm wget ;; \
	    zypper) sudo zypper install -y wget ;; \
	  esac; \
	fi; \
	\
	echo "==> yq"; \
	if command -v yq >/dev/null 2>&1; then \
	  echo "    already installed: $$(yq --version)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install yq ;; \
	    apt) \
	      if command -v snap >/dev/null 2>&1; then sudo snap install yq; \
	      else \
	        YQV=$$(wget -qO- https://api.github.com/repos/mikefarah/yq/releases/latest \
	          | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/'); \
	        ARCH=$$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/'); \
	        sudo wget -qO /usr/local/bin/yq \
	          "https://github.com/mikefarah/yq/releases/download/v$${YQV}/yq_linux_$${ARCH}" \
	          && sudo chmod +x /usr/local/bin/yq; \
	      fi ;; \
	    dnf)    sudo dnf install -y yq ;; \
	    pacman) sudo pacman -S --noconfirm go-yq ;; \
	    zypper) sudo zypper install -y yq ;; \
	  esac; \
	fi; \
	\
	echo "==> Rust"; \
	if command -v cargo >/dev/null 2>&1; then \
	  echo "    already installed: $$(cargo --version)"; \
	else \
	  case "$$PKG" in \
	    homebrew) curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path ;; \
	    *)        wget -qO- https://sh.rustup.rs | sh -s -- -y --no-modify-path ;; \
	  esac; \
	  . "$$HOME/.cargo/env"; \
	fi; \
	. "$$HOME/.cargo/env" 2>/dev/null || true; \
	\
	echo "==> sqlx-cli (required for Rust SQLite compile-time query verification)"; \
	if "$$HOME/.cargo/bin/cargo-sqlx" --version >/dev/null 2>&1 || \
	   command -v cargo-sqlx >/dev/null 2>&1; then \
	  echo "    already installed."; \
	else \
	  . "$$HOME/.cargo/env" 2>/dev/null || true; \
	  cargo install sqlx-cli --no-default-features --features sqlite; \
	fi; \
	\
	echo "==> protoc (required by LanceDB Rust crate)"; \
	if command -v protoc >/dev/null 2>&1; then \
	  echo "    already installed: $$(protoc --version)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install protobuf ;; \
	    apt)      sudo apt-get install -y protobuf-compiler ;; \
	    dnf)      sudo dnf install -y protobuf-compiler ;; \
	    pacman)   sudo pacman -S --noconfirm protobuf ;; \
	    zypper)   sudo zypper install -y protobuf ;; \
	  esac; \
	fi; \
	\
	echo "==> LaTeX (optional — needed for 'make doc')"; \
	if command -v latexmk >/dev/null 2>&1; then \
	  echo "    already installed."; \
	else \
	  echo "    Not installed. To generate ARCHITECTURE.pdf:"; \
	  case "$$PKG" in \
	    homebrew) echo "    brew install --cask mactex" ;; \
	    apt)      echo "    sudo apt-get install -y texlive-full latexmk" ;; \
	    dnf)      echo "    sudo dnf install -y texlive-scheme-full latexmk" ;; \
	    pacman)   echo "    sudo pacman -S --noconfirm texlive-most" ;; \
	    zypper)   echo "    sudo zypper install -y texlive latexmk" ;; \
	  esac; \
	fi; \
	echo "==> System prerequisites done."

# Create both Python venvs and install all packages into each.
# Sentinel files (venv/.installed, loop/scorers/venv/.installed) let Make
# skip reinstallation when nothing has changed.
prereqs-python: venv/.installed loop/scorers/venv/.installed loop/ComfyUI/venv/.installed

venv/.installed: requirements.txt
	python3.11 -m venv venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt
	@touch venv/.installed
	@echo "==> Root venv ready."

# ComfyUI runs in its own venv. Clone the repo if main.py is not present,
# preserving any existing files (launch.sh, workflows/, models/).
loop/ComfyUI/main.py:
	@echo "==> Cloning ComfyUI..."
	git clone --depth=1 https://github.com/comfyanonymous/ComfyUI /tmp/_comfyui_clone
	cp -rn /tmp/_comfyui_clone/. loop/ComfyUI/ || true
	rm -rf /tmp/_comfyui_clone

loop/ComfyUI/venv/.installed: loop/ComfyUI/main.py
	python3.11 -m venv loop/ComfyUI/venv
	loop/ComfyUI/venv/bin/pip install --upgrade pip
	loop/ComfyUI/venv/bin/pip install -r loop/ComfyUI/requirements.txt
	@touch loop/ComfyUI/venv/.installed
	@echo "==> ComfyUI venv ready."

# Install ComfyUI custom nodes by cloning their repos directly.
# Nodes installed:
#   comfyui-inpaint-nodes  — Acly/comfyui-inpaint-nodes (inpainting workflow)
#   comfyui_segment_anything — storyicon/comfyui_segment_anything (mask generation)
comfyui-nodes: $(COMFYUI_NODES_OK)

$(COMFYUI_NODES_OK): loop/ComfyUI/venv/.installed
	@mkdir -p loop/ComfyUI/custom_nodes
	@if [ ! -d loop/ComfyUI/custom_nodes/comfyui-inpaint-nodes ]; then \
	  echo "==> Cloning comfyui-inpaint-nodes..."; \
	  git clone --depth=1 https://github.com/Acly/comfyui-inpaint-nodes \
	    loop/ComfyUI/custom_nodes/comfyui-inpaint-nodes; \
	fi
	@if [ -f loop/ComfyUI/custom_nodes/comfyui-inpaint-nodes/requirements.txt ]; then \
	  loop/ComfyUI/venv/bin/pip install -q \
	    -r loop/ComfyUI/custom_nodes/comfyui-inpaint-nodes/requirements.txt; \
	fi
	@if [ ! -d loop/ComfyUI/custom_nodes/comfyui_segment_anything ]; then \
	  echo "==> Cloning comfyui_segment_anything..."; \
	  git clone --depth=1 https://github.com/storyicon/comfyui_segment_anything \
	    loop/ComfyUI/custom_nodes/comfyui_segment_anything; \
	fi
	@if [ -f loop/ComfyUI/custom_nodes/comfyui_segment_anything/requirements.txt ]; then \
	  loop/ComfyUI/venv/bin/pip install -q \
	    -r loop/ComfyUI/custom_nodes/comfyui_segment_anything/requirements.txt; \
	fi
	@touch $(COMFYUI_NODES_OK)
	@echo "==> ComfyUI custom nodes installed."

# Scorers venv also serves tactical-llm (see tactical-llm/Makefile).
# llama-cpp-python requires platform-specific CMAKE_ARGS for GPU support
# and is installed separately after the rest of the requirements.
loop/scorers/venv/.installed: loop/scorers/requirements.txt
	python3.11 -m venv loop/scorers/venv
	loop/scorers/venv/bin/pip install --upgrade pip
	loop/scorers/venv/bin/pip install -r loop/scorers/requirements.txt
	@echo "==> Installing llama-cpp-python (detecting GPU backend)..."; \
	if [ "$$(uname)" = "Darwin" ]; then \
	  echo "    macOS detected — building with Metal support."; \
	  CMAKE_ARGS="-DGGML_METAL=on" \
	    loop/scorers/venv/bin/pip install llama-cpp-python --no-binary llama-cpp-python; \
	elif command -v nvcc >/dev/null 2>&1; then \
	  echo "    NVIDIA CUDA detected — building with CUDA support."; \
	  CMAKE_ARGS="-DGGML_CUDA=on" \
	    loop/scorers/venv/bin/pip install llama-cpp-python --no-binary llama-cpp-python; \
	elif command -v rocm-smi >/dev/null 2>&1; then \
	  echo "    AMD ROCm detected — building with HIP support."; \
	  CMAKE_ARGS="-DGGML_HIPBLAS=on" \
	    loop/scorers/venv/bin/pip install llama-cpp-python --no-binary llama-cpp-python; \
	else \
	  echo "    No GPU detected — installing CPU-only llama-cpp-python."; \
	  loop/scorers/venv/bin/pip install llama-cpp-python; \
	fi
	@touch loop/scorers/venv/.installed
	@echo "==> Scorers venv ready."

# ── Model downloads ───────────────────────────────────────────────────────────
# Reads model filenames from config.yaml (or config.yaml.default as fallback).
# The SDXL checkpoint must be placed manually — no canonical download URL.

models: venv/.installed
	@VLM_FILE=$$(yq '.models.vlm.filename' $(CFG)); \
	TACTICAL_FILE=$$(yq '.tactical.model.filename' $(CFG)); \
	SCORERS_DIR=loop/scorers; \
	\
	echo "==> Artifact detector (HuggingFace: umm-maybe/AI-image-detector)"; \
	mkdir -p "$$SCORERS_DIR/models/artifact-detector"; \
	venv/bin/python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='umm-maybe/AI-image-detector', \
                  local_dir='$$SCORERS_DIR/models/artifact-detector')"; \
	\
	echo "==> VLM scorer: $$VLM_FILE"; \
	mkdir -p "$$SCORERS_DIR/models/vlm"; \
	if [ -f "$$SCORERS_DIR/models/vlm/$$VLM_FILE" ]; then \
	  echo "    Already present — skipping."; \
	else \
	  curl -L --progress-bar \
	    -o "$$SCORERS_DIR/models/vlm/$$VLM_FILE" \
	    "https://huggingface.co/bartowski/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/$$VLM_FILE"; \
	fi; \
	\
	echo "==> Tactical LLM: $$TACTICAL_FILE"; \
	mkdir -p "$$SCORERS_DIR/models/tactical"; \
	if [ -f "$$SCORERS_DIR/models/tactical/$$TACTICAL_FILE" ]; then \
	  echo "    Already present — skipping."; \
	else \
	  curl -L --progress-bar \
	    -o "$$SCORERS_DIR/models/tactical/$$TACTICAL_FILE" \
	    "https://huggingface.co/bartowski/Qwen3-72B-abliterated-GGUF/resolve/main/$$TACTICAL_FILE"; \
	fi; \
	\
	echo ""; \
	echo "==> Models done."; \
	echo "    SDXL checkpoint: place manually in loop/ComfyUI/models/checkpoints/"

# Interactive HuggingFace model browser.
# Without HF_TOKEN: prints browse URLs and drop locations.
# With HF_TOKEN: launches curses TUI for searching and selecting models.
models-pick: venv/.installed
	venv/bin/python model_picker.py
