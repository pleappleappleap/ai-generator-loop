#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start comfyui_worker "$VENV/bin/python" "$AI_IMAGE_ROOT/loop/comfyui_worker.py" ;;
    stop)    component_stop comfyui_worker ;;
    restart) component_restart comfyui_worker "$VENV/bin/python" "$AI_IMAGE_ROOT/loop/comfyui_worker.py" ;;
    status)  component_status comfyui_worker ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
