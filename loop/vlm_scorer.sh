#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$LOOP_DIR")}"
export SOXHLET_ROOT PYTHONPATH="$SOXHLET_ROOT"
. "$LOOP_DIR/lib/component.sh"

CFG="$SOXHLET_ROOT/config.yaml"
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
    health)
        if component_check_healthy "http://localhost:${PORT}/health"; then
            printf "%-22s %s\n" "vlm_scorer" "healthy"
        else
            printf "%-22s %s\n" "vlm_scorer" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
