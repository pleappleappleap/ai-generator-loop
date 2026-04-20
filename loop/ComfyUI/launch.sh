#!/bin/bash
AI_IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CFG="$AI_IMAGE_ROOT/config.yaml"

COMFYUI=$(yq '.paths.comfyui' "$CFG")
GPU_BACKEND=$(yq '.gpu.backend' "$CFG")

cd "$COMFYUI"
source venv/bin/activate

case "$GPU_BACKEND" in
    mps)  EXTRA_ARGS="--use-mps" ;;
    cuda) EXTRA_ARGS="" ;;
    rocm) EXTRA_ARGS="" ;;
    cpu)  EXTRA_ARGS="--cpu" ;;
    *)    EXTRA_ARGS="" ;;
esac

# shellcheck disable=SC2086
python main.py $EXTRA_ARGS
