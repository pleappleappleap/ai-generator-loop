#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

case "${1:-status}" in
    start)   component_start lancedb_manager "$SCORERS/target/release/lancedb_manager" ;;
    stop)    component_stop lancedb_manager ;;
    restart) component_restart lancedb_manager "$SCORERS/target/release/lancedb_manager" ;;
    status)  component_status lancedb_manager ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
