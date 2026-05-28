#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

CFG="$AI_IMAGE_ROOT/config.yaml"
PORT=$(yq '.sidecars.vlm_scorer.port // 8083' "$CFG" 2>/dev/null)
: "${PORT:=8083}"

case "${1:-status}" in
    start)
        component_start vlm_scorer \
            "$SVENV/bin/uvicorn" vlm_scorer:app \
            --app-dir "$SCORERS" \
            --host 0.0.0.0 --port "$PORT" ;;
    stop)    component_stop vlm_scorer ;;
    restart) component_stop vlm_scorer
             component_start vlm_scorer \
                 "$SVENV/bin/uvicorn" vlm_scorer:app \
                 --app-dir "$SCORERS" \
                 --host 0.0.0.0 --port "$PORT" ;;
    status)  component_status vlm_scorer ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
