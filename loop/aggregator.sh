#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start aggregator "$SCORERS/target/release/aggregator" ;;
    stop)    component_stop aggregator ;;
    restart) component_restart aggregator "$SCORERS/target/release/aggregator" ;;
    status)  component_status aggregator ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
