#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start clip_scorer "$SVENV/bin/python" "$SCORERS/clip_scorer.py" ;;
    stop)    component_stop clip_scorer ;;
    restart) component_restart clip_scorer "$SVENV/bin/python" "$SCORERS/clip_scorer.py" ;;
    status)  component_status clip_scorer ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
