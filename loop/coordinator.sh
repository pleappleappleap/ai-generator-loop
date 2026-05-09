#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start coordinator "$SCORERS/target/release/coordinator" ;;
    stop)    component_stop coordinator ;;
    restart) component_restart coordinator "$SCORERS/target/release/coordinator" ;;
    status)  component_status coordinator ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
