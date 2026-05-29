#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

PIPELINE_JAR="$AI_IMAGE_ROOT/pipeline/target/pipeline.jar"

case "${1:-status}" in
    start)
        if [ ! -f "$PIPELINE_JAR" ]; then
            echo "==> pipeline: JAR not found — run 'make package' in pipeline/" >&2
            exit 1
        fi
        component_start pipeline java -jar "$PIPELINE_JAR"
        ;;
    stop)    component_stop pipeline ;;
    restart)
        component_stop pipeline
        sleep 1
        component_start pipeline java -jar "$PIPELINE_JAR"
        ;;
    status)  component_status pipeline ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
