#!/bin/sh
# Strategic UI component manager — Phase 11 placeholder.
# The strategic UI will be implemented in Phase 11 alongside StrategicLlmCaller.
#
# Usage: ui.sh {start|stop|restart|status|health}

STRATEGIC_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$STRATEGIC_DIR")}"
export SOXHLET_ROOT
. "$SOXHLET_ROOT/loop/lib/component.sh"

case "${1:-status}" in
    start)
        echo "==> strategic_ui: not yet implemented (Phase 11)" >&2
        exit 1 ;;
    stop)    component_stop strategic_ui ;;
    restart) component_stop strategic_ui ;;
    status)  component_status strategic_ui ;;
    health)
        printf "%-22s %s\n" "strategic_ui" "not implemented"
        exit 1 ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
