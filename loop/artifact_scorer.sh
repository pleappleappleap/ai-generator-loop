#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$LOOP_DIR")}"
export SOXHLET_ROOT PYTHONPATH="$SOXHLET_ROOT"
. "$LOOP_DIR/lib/component.sh"

CFG="$SOXHLET_ROOT/config.yaml"
PORT=$(yq '.sidecars.artifact_scorer.port // 12003' "$CFG" 2>/dev/null)
: "${PORT:=12003}"

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
