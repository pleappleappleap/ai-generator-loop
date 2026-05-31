#!/bin/sh
# Strategic UI component manager.
# Starts the strategic review web interface (http://localhost:<port>).
#
# Usage: ui.sh {start|stop|restart|status|health}

STRATEGIC_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$STRATEGIC_DIR")}"
export SOXHLET_ROOT
. "$SOXHLET_ROOT/loop/lib/component.sh"

_resolve() {
    UI_PORT=$(yq '.ui.port // 12000' "$CFG" 2>/dev/null)
    : "${UI_PORT:=12000}"
}

_start() {
    _resolve
    component_start strategic_ui \
        "$SVENV/bin/python" "$STRATEGIC_DIR/server.py"
}

case "${1:-status}" in
    start)   _start ;;
    stop)    component_stop strategic_ui ;;
    restart) component_stop strategic_ui; sleep 1; _start ;;
    status)  component_status strategic_ui ;;
    health)
        _resolve
        if component_check_healthy "http://localhost:${UI_PORT}/"; then
            printf "%-22s %s\n" "strategic_ui" "healthy"
        else
            printf "%-22s %s\n" "strategic_ui" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
