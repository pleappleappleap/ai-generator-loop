#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start router "$SCORERS/target/release/router" ;;
    stop)    component_stop router ;;
    restart) component_restart router "$SCORERS/target/release/router" ;;
    status)  component_status router ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
