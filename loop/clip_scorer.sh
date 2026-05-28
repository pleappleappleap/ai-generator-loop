#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

CFG="$AI_IMAGE_ROOT/config.yaml"
PORT=$(yq '.sidecars.clip_scorer.port // 8081' "$CFG" 2>/dev/null)
: "${PORT:=8081}"

case "${1:-status}" in
    start)
        component_start clip_scorer \
            "$SVENV/bin/uvicorn" clip_scorer:app \
            --app-dir "$SCORERS" \
            --host 0.0.0.0 --port "$PORT" ;;
    stop)    component_stop clip_scorer ;;
    restart) component_stop clip_scorer
             component_start clip_scorer \
                 "$SVENV/bin/uvicorn" clip_scorer:app \
                 --app-dir "$SCORERS" \
                 --host 0.0.0.0 --port "$PORT" ;;
    status)  component_status clip_scorer ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
