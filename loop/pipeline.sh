#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

PIPELINE_DIR="$AI_IMAGE_ROOT/pipeline"
PIPELINE_CLASSES="$PIPELINE_DIR/build/classes"
PIPELINE_LIB="$PIPELINE_DIR/build/lib"

case "${1:-status}" in
    start)
        if [ ! -d "$PIPELINE_CLASSES" ]; then
            echo "==> pipeline: not compiled — run 'make compile' in pipeline/" >&2
            exit 1
        fi
        component_start pipeline java \
            -cp "$PIPELINE_LIB/*:$PIPELINE_CLASSES" \
            ai.image.pipeline.PipelineApplication
        ;;
    stop)    component_stop pipeline ;;
    restart)
        component_stop pipeline
        sleep 1
        component_start pipeline java \
            -cp "$PIPELINE_LIB/*:$PIPELINE_CLASSES" \
            ai.image.pipeline.PipelineApplication
        ;;
    status)  component_status pipeline ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
