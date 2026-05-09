#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start tactical_llm "$VENV/bin/python" "$AI_IMAGE_ROOT/tactical-llm/tactical_llm.py" ;;
    stop)    component_stop tactical_llm ;;
    restart) component_restart tactical_llm "$VENV/bin/python" "$AI_IMAGE_ROOT/tactical-llm/tactical_llm.py" ;;
    status)  component_status tactical_llm ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
