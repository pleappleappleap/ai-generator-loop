#!/bin/sh
# Loop super-component manager.
# Manages the full image-generation loop: ComfyUI, tactical LLM,
# Python ML sidecars, and the Java pipeline.
# Middleware (Artemis + PostgreSQL) is managed separately by middleware.sh at repo root.
#
# Usage: loop.sh {start|stop [--all]|restart|status|health}
#
#   start       Start middleware (if needed) then all loop components.
#               Blocks until every component reports healthy. Does not return
#               until the workflow is ready to accept generation requests.
#   stop        Stop all loop components. Middleware is left running.
#   stop --all  Stop loop components AND middleware.
#   restart     stop then start (middleware stays up).
#   status      Show status of all components.
#   health      Check health of all loop components. Exits 0 if all healthy.

SOXHLET_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOOP_DIR="$SOXHLET_ROOT/loop"
export SOXHLET_ROOT
. "$LOOP_DIR/lib/component.sh"

# ── Config resolution ──────────────────────────────────────────────────────────

_COMFYUI_URL=$(yq '.broker.comfyui_url // "http://127.0.0.1:8188"' "$CFG" 2>/dev/null)
: "${_COMFYUI_URL:=http://127.0.0.1:8188}"

_LLM_URL=$(yq '.tactical.model.server_url // "http://localhost:8080/v1"' "$CFG" 2>/dev/null)
: "${_LLM_URL:=http://localhost:8080/v1}"
# Strip path to get base URL; mlx_lm.server health check is GET /v1/models
_LLM_BASE=$(echo "$_LLM_URL" | sed 's|/v1.*||')

_CLIP_PORT=$(yq '.sidecars.clip_scorer.port // 8081' "$CFG" 2>/dev/null)
: "${_CLIP_PORT:=8081}"

_ARTIFACT_PORT=$(yq '.sidecars.artifact_scorer.port // 8082' "$CFG" 2>/dev/null)
: "${_ARTIFACT_PORT:=8082}"

_VLM_PORT=$(yq '.sidecars.vlm_scorer.port // 8083' "$CFG" 2>/dev/null)
: "${_VLM_PORT:=8083}"

_PIPELINE_HEALTH="http://localhost:8090/actuator/health"

# ── Start ──────────────────────────────────────────────────────────────────────

_stop_strategic_if_running() {
    local pid_file="$PIDDIR/strategic_llm.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "==> Strategic super-component is running — stopping it first..."
            "$SOXHLET_ROOT/strategic.sh" stop
            echo ""
        fi
    fi
}

_start() {
    _stop_strategic_if_running
    echo "==> Starting middleware..."
    "$SOXHLET_ROOT/middleware.sh" start

    echo ""
    echo "==> Starting ComfyUI and tactical LLM..."
    "$LOOP_DIR/comfyui.sh" start
    "$LOOP_DIR/tactical_llm.sh" start
    component_wait_healthy "${_COMFYUI_URL}/system_stats"  "ComfyUI"      180
    component_wait_healthy "${_LLM_BASE}/v1/models"        "tactical_llm" 300

    echo ""
    echo "==> Starting Python ML sidecars..."
    "$LOOP_DIR/clip_scorer.sh" start
    "$LOOP_DIR/artifact_scorer.sh" start
    "$LOOP_DIR/vlm_scorer.sh" start
    component_wait_healthy "http://localhost:${_CLIP_PORT}/health"     "clip-scorer"     60
    component_wait_healthy "http://localhost:${_ARTIFACT_PORT}/health" "artifact-scorer" 60
    component_wait_healthy "http://localhost:${_VLM_PORT}/health"      "vlm-scorer"      120

    echo ""
    echo "==> Starting Java pipeline..."
    "$LOOP_DIR/pipeline.sh" start
    component_wait_healthy "$_PIPELINE_HEALTH" "pipeline" 120 '"UP"'

    echo ""
    _status
}

# ── Stop ───────────────────────────────────────────────────────────────────────

_stop() {
    echo "==> Stopping Java pipeline..."
    "$LOOP_DIR/pipeline.sh" stop

    echo "==> Stopping Python ML sidecars..."
    "$LOOP_DIR/clip_scorer.sh" stop
    "$LOOP_DIR/artifact_scorer.sh" stop
    "$LOOP_DIR/vlm_scorer.sh" stop

    echo "==> Stopping ComfyUI and tactical LLM..."
    "$LOOP_DIR/comfyui.sh" stop
    "$LOOP_DIR/tactical_llm.sh" stop

    echo "==> Loop stopped. Middleware (Artemis + PostgreSQL) is still running."
    echo "    Use 'loop.sh stop --all' or 'middleware.sh stop --all' to tear down middleware."
}

_stop_all() {
    _stop
    echo ""
    echo "==> Stopping middleware..."
    "$SOXHLET_ROOT/middleware.sh" stop --all
}

# ── Status ─────────────────────────────────────────────────────────────────────

_status() {
    echo "┌─ Middleware ──────────────────────────────────────────┐"
    "$SOXHLET_ROOT/middleware.sh" health 2>/dev/null || "$SOXHLET_ROOT/middleware.sh" status
    echo "├─ Loop ────────────────────────────────────────────────┤"
    "$LOOP_DIR/comfyui.sh" status
    "$LOOP_DIR/tactical_llm.sh" status
    "$LOOP_DIR/clip_scorer.sh" status
    "$LOOP_DIR/artifact_scorer.sh" status
    "$LOOP_DIR/vlm_scorer.sh" status
    "$LOOP_DIR/pipeline.sh" status
    echo "└───────────────────────────────────────────────────────┘"
    echo ""
    echo "Logs: /tmp/ai-loop/<component>.log"
    echo "UI:   http://localhost:8090"
}

# ── Health ─────────────────────────────────────────────────────────────────────

_health() {
    local ok=0
    "$SOXHLET_ROOT/middleware.sh" health || ok=1
    component_check_healthy "${_COMFYUI_URL}/system_stats" \
        && printf "%-22s %s\n" "comfyui" "healthy" \
        || { printf "%-22s %s\n" "comfyui" "not ready"; ok=1; }
    component_check_healthy "${_LLM_BASE}/v1/models" \
        && printf "%-22s %s\n" "tactical_llm" "healthy" \
        || { printf "%-22s %s\n" "tactical_llm" "not ready"; ok=1; }
    component_check_healthy "http://localhost:${_CLIP_PORT}/health" \
        && printf "%-22s %s\n" "clip_scorer" "healthy" \
        || { printf "%-22s %s\n" "clip_scorer" "not ready"; ok=1; }
    component_check_healthy "http://localhost:${_ARTIFACT_PORT}/health" \
        && printf "%-22s %s\n" "artifact_scorer" "healthy" \
        || { printf "%-22s %s\n" "artifact_scorer" "not ready"; ok=1; }
    component_check_healthy "http://localhost:${_VLM_PORT}/health" \
        && printf "%-22s %s\n" "vlm_scorer" "healthy" \
        || { printf "%-22s %s\n" "vlm_scorer" "not ready"; ok=1; }
    component_check_healthy "$_PIPELINE_HEALTH" '"UP"' \
        && printf "%-22s %s\n" "pipeline" "healthy" \
        || { printf "%-22s %s\n" "pipeline" "not ready"; ok=1; }
    return $ok
}

# ── Dispatch ───────────────────────────────────────────────────────────────────

case "${1:-status}" in
    start)   _start ;;
    stop)
        case "${2:-}" in
            --all) _stop_all ;;
            *)     _stop ;;
        esac ;;
    restart) _stop; echo ""; _start ;;
    status)  _status ;;
    health)  _health ;;
    *)
        echo "Usage: $0 {start|stop [--all]|restart|status|health}" >&2
        exit 1 ;;
esac
