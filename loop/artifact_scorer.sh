#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT PYTHONPATH="$AI_IMAGE_ROOT"
. "$LOOP_DIR/lib/component.sh"

CFG="$AI_IMAGE_ROOT/config.yaml"
PORT=$(yq '.sidecars.artifact_scorer.port // 8082' "$CFG" 2>/dev/null)
: "${PORT:=8082}"

case "${1:-status}" in
    start)
        component_start artifact_scorer \
            "$SVENV/bin/uvicorn" artifact_scorer:app \
            --app-dir "$SCORERS" \
            --host 0.0.0.0 --port "$PORT" ;;
    stop)    component_stop artifact_scorer ;;
    restart) component_stop artifact_scorer
             component_start artifact_scorer \
                 "$SVENV/bin/uvicorn" artifact_scorer:app \
                 --app-dir "$SCORERS" \
                 --host 0.0.0.0 --port "$PORT" ;;
    status)  component_status artifact_scorer ;;
    health)
        if component_check_healthy "http://localhost:${PORT}/health"; then
            printf "%-22s %s\n" "artifact_scorer" "healthy"
        else
            printf "%-22s %s\n" "artifact_scorer" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
