#!/bin/sh
SOXHLET_ROOT="${1:-${SOXHLET_ROOT:-}}"
if [ -z "$SOXHLET_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    SOXHLET_ROOT="$PWD"
fi
: "${SOXHLET_ROOT:?provide root as \$1, set SOXHLET_ROOT in environment, or run from the repo root}"
CFG="$SOXHLET_ROOT/config.yaml"

COMFYUI=$(yq '.paths.comfyui' "$CFG")
[ "$COMFYUI" = "auto" ] && COMFYUI="$SOXHLET_ROOT/loop/ComfyUI"

BACKEND=$(yq '.compute.comfyui.backend' "$CFG")
LISTEN=$(yq '.compute.comfyui.listen' "$CFG")
DEVICE_ID=$(yq '.compute.comfyui.device_id' "$CFG")
# yq emits "null" for absent keys; normalise to empty
[ "$LISTEN"    = "null" ] && LISTEN="0.0.0.0"
[ "$DEVICE_ID" = "null" ] && DEVICE_ID=""

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
. venv/bin/activate

# --default-device keeps all devices visible but sets the preferred one.
# Only passed when device_id is explicitly set in config.
case "$BACKEND" in
    mps)  EXTRA_ARGS="" ;;
    cuda) EXTRA_ARGS="${DEVICE_ID:+--default-device $DEVICE_ID}" ;;
    rocm) EXTRA_ARGS="${DEVICE_ID:+--default-device $DEVICE_ID}" ;;
    cpu)  EXTRA_ARGS="--cpu" ;;
    *)    EXTRA_ARGS="" ;;
esac

# When binding to all interfaces, print the real hostname so the URL is useful.
if [ "$LISTEN" = "0.0.0.0" ]; then
    _HOST=$(hostname 2>/dev/null || echo "localhost")
    echo "==> ComfyUI available at: http://${_HOST}:8188"
fi

# shellcheck disable=SC2086
exec python main.py --listen "$LISTEN" $EXTRA_ARGS
