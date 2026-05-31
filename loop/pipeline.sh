#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$LOOP_DIR")}"
export SOXHLET_ROOT
. "$LOOP_DIR/lib/component.sh"

PIPELINE_DIR="$SOXHLET_ROOT/pipeline"
PIPELINE_CLASSES="$PIPELINE_DIR/build/classes"
PIPELINE_LIB="$PIPELINE_DIR/build/lib"

# Propagate broker.url from config.yaml → SPRING_ARTEMIS_BROKER_URL (if yq is available)
if command -v yq >/dev/null 2>&1 && [ -f "$SOXHLET_ROOT/config.yaml" ]; then
    _BROKER_URL=$(yq '.broker.url' "$SOXHLET_ROOT/config.yaml" 2>/dev/null)
    if [ -n "$_BROKER_URL" ] && [ "$_BROKER_URL" != "null" ]; then
        export SPRING_ARTEMIS_BROKER_URL="$_BROKER_URL"
    fi
fi

case "${1:-status}" in
    start)
        if [ ! -d "$PIPELINE_CLASSES" ]; then
            echo "==> pipeline: not compiled — run 'make compile' in pipeline/" >&2
            exit 1
        fi
        component_start pipeline java \
            -cp "$PIPELINE_LIB/*:$PIPELINE_CLASSES" \
            org.soxhlet.pipeline.PipelineApplication
        ;;
    stop)    component_stop pipeline ;;
    restart)
        component_stop pipeline
        sleep 1
        component_start pipeline java \
            -cp "$PIPELINE_LIB/*:$PIPELINE_CLASSES" \
            org.soxhlet.pipeline.PipelineApplication
        ;;
    status)  component_status pipeline ;;
    health)
        if component_check_healthy "http://localhost:12000/actuator/health" '"UP"'; then
            printf "%-22s %s\n" "pipeline" "healthy"
        else
            printf "%-22s %s\n" "pipeline" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
