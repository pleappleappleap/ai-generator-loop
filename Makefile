.PHONY: all lint typecheck test build format doc clean config check \
        prereqs prereqs-system prereqs-python models

# Use generated config.yaml if present, otherwise fall back to the template.
CFG := $(or $(wildcard config.yaml),config.yaml.default)

all: lint typecheck test build

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

doc:
	latexmk -pdf -interaction=nonstopmode ARCHITECTURE.tex
	$(MAKE) -C loop doc
	$(MAKE) -C tactical-llm doc

clean:
	latexmk -C ARCHITECTURE.tex
	rm -f *.aux *.log *.out *.toc *.fls *.fdb_latexmk
	$(MAKE) -C loop clean
	$(MAKE) -C tactical-llm clean

# Interactive configuration TUI (menuconfig-style).
# Generates config.yaml and config.sh from auto-detected defaults.
config:
	@command -v yq >/dev/null 2>&1 || { echo "yq not found — brew install yq"; exit 1; }
	python3 menuconfig.py

# Validate the environment against the current config.yaml.
# Checks services, GPU backend, model files, venvs, and Rust binaries.
check: config.yaml
	python3 check_env.py

config.yaml:
	@echo "config.yaml not found — running make config first"
	$(MAKE) config

# ── Prerequisites ─────────────────────────────────────────────────────────────

prereqs: prereqs-system prereqs-python

# Install system-level packages using the detected package manager.
# Installs: Python 3.11, yq, Java (for Artemis), Rust (via rustup).
# LaTeX is optional and only needed for `make doc`.
prereqs-system:
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
	echo "==> yq"; \
	if command -v yq >/dev/null 2>&1; then \
	  echo "    already installed: $$(yq --version)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install yq ;; \
	    apt) \
	      if command -v snap >/dev/null 2>&1; then sudo snap install yq; \
	      else \
	        YQV=$$(curl -s https://api.github.com/repos/mikefarah/yq/releases/latest \
	          | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/'); \
	        sudo curl -L "https://github.com/mikefarah/yq/releases/download/v$${YQV}/yq_linux_amd64" \
	          -o /usr/local/bin/yq && sudo chmod +x /usr/local/bin/yq; \
	      fi ;; \
	    dnf)    sudo dnf install -y yq ;; \
	    pacman) sudo pacman -S --noconfirm go-yq ;; \
	    zypper) sudo zypper install -y yq ;; \
	  esac; \
	fi; \
	\
	echo "==> Java (required by ActiveMQ Artemis)"; \
	if command -v java >/dev/null 2>&1; then \
	  echo "    already installed: $$(java -version 2>&1 | head -1)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install openjdk ;; \
	    apt)      sudo apt-get install -y default-jdk ;; \
	    dnf)      sudo dnf install -y java-latest-openjdk ;; \
	    pacman)   sudo pacman -S --noconfirm jre-openjdk ;; \
	    zypper)   sudo zypper install -y java-openjdk ;; \
	  esac; \
	fi; \
	\
	echo "==> Rust"; \
	if command -v cargo >/dev/null 2>&1; then \
	  echo "    already installed: $$(cargo --version)"; \
	else \
	  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path; \
	  . "$$HOME/.cargo/env"; \
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
prereqs-python: venv/.installed loop/scorers/venv/.installed

venv/.installed: requirements.txt
	python3.11 -m venv venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt
	@touch venv/.installed
	@echo "==> Root venv ready."

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
	  CMAKE_ARGS="-DLLAMA_METAL=on" \
	    loop/scorers/venv/bin/pip install llama-cpp-python --no-binary llama-cpp-python; \
	elif command -v nvcc >/dev/null 2>&1; then \
	  echo "    NVIDIA CUDA detected — building with CUDA support."; \
	  CMAKE_ARGS="-DLLAMA_CUDA=on" \
	    loop/scorers/venv/bin/pip install llama-cpp-python --no-binary llama-cpp-python; \
	elif command -v rocm-smi >/dev/null 2>&1; then \
	  echo "    AMD ROCm detected — building with HIP support."; \
	  CMAKE_ARGS="-DLLAMA_HIPBLAS=on" \
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
