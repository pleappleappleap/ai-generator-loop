# Git Bash on Windows sets MSYSTEM (e.g. MINGW64); CMD/PowerShell does not.
# Only block CMD/PowerShell — Git Bash users can proceed normally.
ifeq ($(OS),Windows_NT)
  ifndef MSYSTEM
    $(error Windows CMD/PowerShell detected. Run from Git Bash instead: https://git-scm.com/downloads)
  endif
endif

.PHONY: all lint typecheck test build format docs clean distclean config \
        prereqs prereqs-system prereqs-python models models-pick setup \
        k8s-install comfyui-nodes targets

targets:
	@printf "%-20s %s\n" "all"            "Install venvs, then lint, type-check, test, and compile."
	@printf "%-20s %s\n" "lint"           "Lint loop/ and loop/tactical-llm/ with ruff."
	@printf "%-20s %s\n" "typecheck"      "Type-check loop/ and loop/tactical-llm/ with mypy."
	@printf "%-20s %s\n" "test"           "Run all tests: pytest on loop/ and tactical-llm/, plus Maven on pipeline/."
	@printf "%-20s %s\n" "build"          "Compile the Java pipeline (mvn compile in pipeline/)."
	@printf "%-20s %s\n" "format"         "Format loop/ with ruff."
	@printf "%-20s %s\n" "docs"           "Build ARCHITECTURE.pdf from ARCHITECTURE.tex with latexmk."
	@printf "%-20s %s\n" "clean"          "Remove build artifacts and caches."
	@printf "%-20s %s\n" "distclean"      "Remove artifacts, venvs, the ComfyUI clone, and downloaded models."
	@printf "%-20s %s\n" "config"         "Generate config.yaml via the interactive menuconfig.py TUI."
	@printf "%-20s %s\n" "setup"          "Full first-time setup: prereqs, config, models, then build."
	@printf "%-20s %s\n" "prereqs"        "Run prereqs-system and prereqs-python."
	@printf "%-20s %s\n" "prereqs-system" "Install Python 3.11, yq, Java 21, and the HuggingFace CLI."
	@printf "%-20s %s\n" "prereqs-python" "Create all Python venvs: root, scorers, ComfyUI, and tactical-llm."
	@printf "%-20s %s\n" "k8s-install"    "Install Colima, K3s, or Rancher Desktop if no cluster is present."
	@printf "%-20s %s\n" "comfyui-nodes"  "Clone inpaint-nodes and segment_anything into ComfyUI/custom_nodes/."
	@printf "%-20s %s\n" "models"         "Download all models via scripts/download_models.py."
	@printf "%-20s %s\n" "models-pick"    "Launch the interactive HuggingFace model browser."
	@printf "%-20s %s\n" "targets"        "Show this list."

# Use generated config.yaml if present, otherwise fall back to the template.
CFG := $(or $(wildcard config.yaml),config.yaml.default)

K8S_OK           := loop/.k8s-installed
COMFYUI_NODES_OK := loop/ComfyUI/custom_nodes/.nodes-installed

all: venv/.installed loop/scorers/venv/.installed loop/tactical-llm/venv/.installed \
     $(COMFYUI_NODES_OK) lint typecheck test build

lint:
	$(MAKE) -C loop lint
	$(MAKE) -C loop/tactical-llm lint

typecheck:
	$(MAKE) -C loop typecheck
	$(MAKE) -C loop/tactical-llm typecheck

test:
	$(MAKE) -C loop test
	$(MAKE) -C loop/tactical-llm test
	$(MAKE) -C pipeline test

build:
	$(MAKE) -C pipeline compile

format:
	$(MAKE) -C loop format

docs:
	latexmk -pdf -interaction=nonstopmode ARCHITECTURE.tex
	$(MAKE) -C loop docs
	$(MAKE) -C loop/tactical-llm docs

clean:
	latexmk -C ARCHITECTURE.tex
	rm -f *.aux *.log *.out *.toc *.fls *.fdb_latexmk
	rm -rf __pycache__ .pytest_cache .ruff_cache
	$(MAKE) -C loop clean
	$(MAKE) -C loop/tactical-llm clean
	$(MAKE) -C pipeline clean

# Auto-downloaded model directories are declared in .gitignore.
# distclean reads that file directly — no git command required — so adding a
# new model directory to .gitignore is sufficient to make distclean handle it.
# Within each such directory a .gitkeep sentinel (if present) is preserved so
# the placeholder survives and make models knows where to put files.
MODEL_DIRS := $(shell grep -E '^loop/scorers/models/|^strategic-llm/models' .gitignore | sed 's|/*$$||')

# Remove everything that can be regenerated: venvs, the ComfyUI clone, and
# auto-downloaded model files.  Reads .gitignore and .gitkeep files as the
# source of truth — does not require the git CLI or a .git directory.
# The only hardcoded exclusions protect user-placed data that cannot be
# re-downloaded (SDXL checkpoints, custom workflows).
# After distclean, restore with: make setup
distclean: clean
	@echo "==> Removing Python virtual environments..."
	rm -rf venv loop/scorers/venv loop/tactical-llm/venv
	@echo "==> Removing auto-downloaded model files (preserving .gitkeep sentinels)..."
	@for d in $(MODEL_DIRS); do \
	  if [ -d "$$d" ]; then \
	    echo "    cleaning $$d"; \
	    keep=0; [ -f "$$d/.gitkeep" ] && keep=1; \
	    rm -rf "$$d"; \
	    mkdir -p "$$d"; \
	    [ "$$keep" = "1" ] && touch "$$d/.gitkeep"; \
	  fi; \
	done
	@echo "==> Removing ComfyUI clone (including models)..."
	rm -rf loop/ComfyUI
	git checkout -- loop/ComfyUI/
	@echo "==> distclean complete. Run 'make setup' to rebuild from scratch."

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

config.yaml:
	@echo "config.yaml not found — running make config first"
	$(MAKE) config

# ── Prerequisites ─────────────────────────────────────────────────────────────

# ── Kubernetes install ────────────────────────────────────────────────────────
#
# Artemis and PostgreSQL run as K3s pods (kubectl apply -k k8s/).
# This target ensures a working kubectl + cluster is available.
#
# macOS:   Colima with the --kubernetes flag (embeds K3s in a lightweight VM).
# Linux:   K3s via the official install script; sets up kubeconfig for the user.
# Windows: Rancher Desktop via winget (bundles K3s + kubectl; no WSL2 required).
k8s-install: $(K8S_OK)

$(K8S_OK):
	@_k8s_wait() { \
	  echo "==> Waiting for Kubernetes API (up to 120 s)..."; \
	  n=0; \
	  while ! kubectl get nodes >/dev/null 2>&1; do \
	    n=$$((n+1)); \
	    if [ $$n -ge 60 ]; then \
	      echo "ERROR: Kubernetes API did not respond within 120 s." >&2; \
	      echo "       Check cluster status and re-run make." >&2; \
	      exit 1; \
	    fi; \
	    sleep 2; \
	  done; \
	  echo "==> Kubernetes ready."; \
	}; \
	if kubectl get nodes >/dev/null 2>&1; then \
	  echo "==> Kubernetes already running: $$(kubectl version --client --short 2>/dev/null | head -1)"; \
	elif command -v brew >/dev/null 2>&1; then \
	  echo "==> Installing colima + kubectl via Homebrew..."; \
	  brew install colima kubectl; \
	  echo "==> Starting Colima with Kubernetes (K3s)..."; \
	  colima start --kubernetes 2>/dev/null \
	    || colima stop 2>/dev/null; colima start --kubernetes; \
	  _k8s_wait; \
	elif [ "$$(uname -s)" = "Linux" ]; then \
	  echo "==> Installing K3s..."; \
	  curl -sfL https://get.k3s.io | sh -; \
	  echo "==> Configuring kubeconfig..."; \
	  mkdir -p "$$HOME/.kube"; \
	  sudo cp /etc/rancher/k3s/k3s.yaml "$$HOME/.kube/config"; \
	  sudo chown "$$(id -u):$$(id -g)" "$$HOME/.kube/config"; \
	  _k8s_wait; \
	elif command -v winget >/dev/null 2>&1; then \
	  echo "==> Installing Rancher Desktop via winget (includes K3s + kubectl)..."; \
	  winget install --id Rancher.RancherDesktop -e \
	    --accept-package-agreements --accept-source-agreements; \
	  echo "NOTE: Rancher Desktop needs a moment to start its K3s cluster."; \
	  echo "      Re-run 'make prereqs-system' after it has fully started."; \
	  _k8s_wait; \
	else \
	  echo "ERROR: Cannot auto-install Kubernetes on this platform." >&2; \
	  echo "       Install kubectl + a K3s provider (Colima, Rancher Desktop, or K3s) manually." >&2; \
	  exit 1; \
	fi
	@mkdir -p loop
	@touch $(K8S_OK)

# Full first-time setup: system deps → Python venvs → config → models → build.
setup: prereqs-system prereqs-python config models build

prereqs: prereqs-system prereqs-python

# Install system-level packages using the detected package manager.
# Installs: wget (Linux), Python 3.11, yq, Java 21, HuggingFace CLI.
# LaTeX is optional and only needed for `make docs`.
prereqs-system: k8s-install
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
	echo "==> Java 21 (required for the pipeline application)"; \
	if java -version 2>&1 | grep -q 'version "2[1-9]\|version "[3-9][0-9]'; then \
	  echo "    already installed: $$(java -version 2>&1 | head -1)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install openjdk@21 ;; \
	    apt)      sudo apt-get install -y openjdk-21-jdk ;; \
	    dnf)      sudo dnf install -y java-21-openjdk-devel ;; \
	    pacman)   sudo pacman -S --noconfirm jdk21-openjdk ;; \
	    zypper)   sudo zypper install -y java-21-openjdk-devel ;; \
	  esac; \
	fi; \
	\
	echo "==> hf (HuggingFace CLI — required for model downloads)"; \
	if command -v hf >/dev/null 2>&1; then \
	  echo "    already installed: $$(hf --version 2>/dev/null || echo ok)"; \
	else \
	  case "$$PKG" in \
	    homebrew) brew install huggingface-cli ;; \
	    apt|dnf|pacman|zypper) \
	      pip3 install --user 'huggingface_hub[cli]'; \
	      echo "NOTE: ensure ~/.local/bin is in your PATH for the hf command." ;; \
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
prereqs-python: venv/.installed loop/scorers/venv/.installed loop/ComfyUI/venv/.installed \
                loop/tactical-llm/venv/.installed

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

loop/tactical-llm/venv/.installed:
	$(MAKE) -C loop/tactical-llm venv
	@touch loop/tactical-llm/venv/.installed
	@echo "==> tactical-llm venv ready."

# mlx-lm is Apple Silicon only and installed separately after the rest of the requirements.
loop/scorers/venv/.installed: loop/scorers/requirements.txt
	python3.11 -m venv loop/scorers/venv
	loop/scorers/venv/bin/pip install --upgrade pip
	loop/scorers/venv/bin/pip install -r loop/scorers/requirements.txt
	@echo "==> Installing MLX packages (Apple Silicon — mlx-lm + mlx-vlm)..."; \
	if [ "$$(uname)" = "Darwin" ]; then \
	  loop/scorers/venv/bin/pip install mlx-lm mlx-vlm; \
	else \
	  echo "    NOTE: mlx-lm and mlx-vlm are Apple Silicon only — skipping." >&2; \
	fi
	@touch loop/scorers/venv/.installed
	@echo "==> Scorers venv ready."

# ── Model downloads ───────────────────────────────────────────────────────────
# Reads model filenames from config.yaml (or config.yaml.default as fallback).
# The SDXL checkpoint must be placed manually — no canonical download URL.

models: venv/.installed
	@venv/bin/python scripts/download_models.py

# Interactive HuggingFace model browser.
# Without HF_TOKEN: prints browse URLs and drop locations.
# With HF_TOKEN: launches curses TUI for searching and selecting models.
models-pick: venv/.installed
	venv/bin/python model_picker.py
