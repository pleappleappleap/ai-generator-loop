#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start comfyui "$COMFYUI_DIR/launch.sh" ;;
    stop)    component_stop comfyui ;;
    restart) component_restart comfyui "$COMFYUI_DIR/launch.sh" ;;
    status)  component_status comfyui ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
