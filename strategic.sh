#!/bin/sh
# Strategic super-component manager.
# Manages the strategic review session: Strategic LLM + strategic UI.
# Loop and Strategic are mutually exclusive — Loop must be stopped before starting Strategic.
# Middleware (Artemis + PostgreSQL) must be running.
#
# Usage: strategic.sh {start|stop|restart|status|health}

SOXHLET_ROOT="$(cd "$(dirname "$0")" && pwd)"
STRATEGIC_DIR="$SOXHLET_ROOT/strategic-llm"
export SOXHLET_ROOT
. "$SOXHLET_ROOT/loop/lib/component.sh"

_LLM_SERVER_URL=$(yq '.strategic.model.server_url // "http://localhost:8081/v1"' "$CFG" 2>/dev/null)
: "${_LLM_SERVER_URL:=http://localhost:8081/v1}"
_LLM_BASE=$(echo "$_LLM_SERVER_URL" | sed 's|/v1.*||')

_UI_PORT=$(yq '.ui.port // 7860' "$CFG" 2>/dev/null)
: "${_UI_PORT:=7860}"

_stop_loop_if_running() {
    local pid_file="$PIDDIR/pipeline.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "==> Loop super-component is running — stopping it first..."
            "$SOXHLET_ROOT/loop.sh" stop
            echo ""
        fi
    fi
}

_start() {
    _stop_loop_if_running

    echo "==> Starting strategic LLM server..."
    "$STRATEGIC_DIR/strategic_llm.sh" start
    component_wait_healthy "${_LLM_BASE}/v1/models" "strategic_llm" 300

    echo ""
    echo "==> Starting strategic UI..."
    "$STRATEGIC_DIR/ui.sh" start
    component_wait_healthy "http://localhost:${_UI_PORT}/" "strategic_ui" 60

    echo ""
    _status
}

_stop() {
    echo "==> Stopping strategic UI..."
    "$STRATEGIC_DIR/ui.sh" stop
    echo "==> Stopping strategic LLM server..."
    "$STRATEGIC_DIR/strategic_llm.sh" stop
    echo "==> Strategic super-component stopped."
}

_status() {
    echo "┌─ Strategic ───────────────────────────────────────────┐"
    "$STRATEGIC_DIR/strategic_llm.sh" status
    "$STRATEGIC_DIR/ui.sh" status
    echo "└───────────────────────────────────────────────────────┘"
}

_health() {
    local ok=0
    component_check_healthy "${_LLM_BASE}/v1/models" \
        && printf "%-22s %s\n" "strategic_llm" "healthy" \
        || { printf "%-22s %s\n" "strategic_llm" "not ready"; ok=1; }
    component_check_healthy "http://localhost:${_UI_PORT}/" \
        && printf "%-22s %s\n" "strategic_ui" "healthy" \
        || { printf "%-22s %s\n" "strategic_ui" "not ready"; ok=1; }
    return $ok
}

case "${1:-status}" in
    start)   _start ;;
    stop)    _stop ;;
    restart) _stop; echo ""; _start ;;
    status)  _status ;;
    health)  _health ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
