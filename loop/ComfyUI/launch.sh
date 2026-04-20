#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../config.sh
source "$SCRIPT_DIR/../../config.sh"

cd "$AI_IMAGE_COMFYUI"
source venv/bin/activate

case "$AI_IMAGE_GPU_BACKEND" in
    mps)  EXTRA_ARGS="--use-mps" ;;
    cuda) EXTRA_ARGS="" ;;
    rocm) EXTRA_ARGS="" ;;
    cpu)  EXTRA_ARGS="--cpu" ;;
    *)    EXTRA_ARGS="" ;;
esac

# shellcheck disable=SC2086
python main.py $EXTRA_ARGS
