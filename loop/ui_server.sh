#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start ui_server "$VENV/bin/python" "$AI_IMAGE_ROOT/loop/ui/server.py" ;;
    stop)    component_stop ui_server ;;
    restart) component_restart ui_server "$VENV/bin/python" "$AI_IMAGE_ROOT/loop/ui/server.py" ;;
    status)  component_status ui_server ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
