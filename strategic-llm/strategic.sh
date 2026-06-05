#!/bin/sh
# Strategic super-component manager.
# Manages the strategic review session: Strategic LLM + pipeline (REST API).
# The strategic UI is served by the pipeline at /strategic/.
# Loop and Strategic are mutually exclusive — Loop must be stopped before starting Strategic.
# Middleware (Artemis + PostgreSQL) must be running.
#
# Usage: strategic.sh {start|stop|restart|status|health}

STRATEGIC_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="$(cd "$STRATEGIC_DIR/.." && pwd)"
LOOP_DIR="$SOXHLET_ROOT/loop"
export SOXHLET_ROOT
. "$LOOP_DIR/lib/component.sh"

_LLM_SERVER_URL=$(yq '.strategic.model.server_url // "http://localhost:12005/v1"' "$CFG" 2>/dev/null)
: "${_LLM_SERVER_URL:=http://localhost:12005/v1}"
_LLM_BASE=$(echo "$_LLM_SERVER_URL" | sed 's|/v1.*||')

_PIPELINE_HEALTH="http://localhost:12000/actuator/health"

_stop_loop_if_running() {
    local pid_file="$PIDDIR/pipeline.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "==> Loop super-component is running — stopping it first..."
            "$SOXHLET_ROOT/loop/loop.sh" stop
            echo ""
        fi
    fi
}

_start() {
    _stop_loop_if_running
    echo "==> Starting middleware..."
    "$SOXHLET_ROOT/middleware.sh" start

    echo ""
    echo "==> Starting strategic LLM server..."
    "$STRATEGIC_DIR/strategic_llm.sh" start
    component_wait_healthy "${_LLM_BASE}/v1/models" "strategic_llm" 300

    echo ""
    echo "==> Starting pipeline (REST API + strategic session handler)..."
    "$LOOP_DIR/pipeline.sh" start
    component_wait_healthy "$_PIPELINE_HEALTH" "pipeline" 120 '"UP"'

    echo ""
    echo "==> Strategic UI: http://localhost:12000/strategic/"
    _status
}

_stop() {
    echo "==> Stopping pipeline..."
    "$LOOP_DIR/pipeline.sh" stop
    echo "==> Stopping strategic LLM server..."
    "$STRATEGIC_DIR/strategic_llm.sh" stop
    echo "==> Strategic super-component stopped."
}

_status() {
    echo "┌─ Strategic ───────────────────────────────────────────┐"
    "$STRATEGIC_DIR/strategic_llm.sh" status
    "$LOOP_DIR/pipeline.sh" status
    echo "└───────────────────────────────────────────────────────┘"
}

_health() {
    local ok=0
    component_check_healthy "${_LLM_BASE}/v1/models" \
        && printf "%-22s %s\n" "strategic_llm" "healthy" \
        || { printf "%-22s %s\n" "strategic_llm" "not ready"; ok=1; }
    component_check_healthy "$_PIPELINE_HEALTH" '"UP"' \
        && printf "%-22s %s\n" "pipeline" "healthy" \
        || { printf "%-22s %s\n" "pipeline" "not ready"; ok=1; }
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
