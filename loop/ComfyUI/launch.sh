#!/bin/bash
AI_IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CFG="$AI_IMAGE_ROOT/config.yaml"

COMFYUI=$(yq '.paths.comfyui' "$CFG")
BACKEND=$(yq '.compute.comfyui.backend' "$CFG")

# Resolve auto to a concrete backend
if [ "$BACKEND" = "auto" ]; then
    if [ "$(uname)" = "Darwin" ]; then
        BACKEND="mps"
    elif command -v nvcc >/dev/null 2>&1 || command -v nvidia-smi >/dev/null 2>&1; then
        BACKEND="cuda"
    elif command -v rocm-smi >/dev/null 2>&1; then
        BACKEND="rocm"
    else
        BACKEND="cpu"
    fi
fi

cd "$COMFYUI"
source venv/bin/activate

case "$BACKEND" in
    mps)  EXTRA_ARGS="--use-mps" ;;
    cuda) EXTRA_ARGS="" ;;
    rocm) EXTRA_ARGS="" ;;
    cpu)  EXTRA_ARGS="--cpu" ;;
    *)    EXTRA_ARGS="" ;;
esac

# shellcheck disable=SC2086
python main.py $EXTRA_ARGS
