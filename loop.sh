#!/bin/sh
# Loop super-component manager.
# Manages the full image-generation loop: ComfyUI, tactical LLM,
# Python ML sidecars, and the Java pipeline.
# Middleware (Artemis + PostgreSQL) is managed separately by middleware.sh at repo root.
#
# Usage: loop.sh {start [--auto-escalate]|stop [--all]|restart|status|health}
#
#   start [--auto-escalate]
#               Start middleware (if needed) then all loop components.
#               Blocks until every component reports healthy. Does not return
#               until the workflow is ready to accept generation requests.
#               With --auto-escalate: forks a background monitor that hands off
#               to strategic.sh automatically when the pipeline exits with a
#               pending escalation in pipeline_events. Without this flag the
#               operator controls when (and whether) to start strategic.sh.
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

_COMFYUI_URL=$(yq '.broker.comfyui_url // "http://127.0.0.1:12006"' "$CFG" 2>/dev/null)
: "${_COMFYUI_URL:=http://127.0.0.1:12006}"

_LLM_URL=$(yq '.tactical.model.server_url // "http://localhost:12001/v1"' "$CFG" 2>/dev/null)
: "${_LLM_URL:=http://localhost:12001/v1}"
# Strip path to get base URL; mlx_lm.server health check is GET /v1/models
_LLM_BASE=$(echo "$_LLM_URL" | sed 's|/v1.*||')

_CLIP_PORT=$(yq '.sidecars.clip_scorer.port // 12002' "$CFG" 2>/dev/null)
: "${_CLIP_PORT:=12002}"

_ARTIFACT_PORT=$(yq '.sidecars.artifact_scorer.port // 12003' "$CFG" 2>/dev/null)
: "${_ARTIFACT_PORT:=12003}"

_VLM_PORT=$(yq '.sidecars.vlm_scorer.port // 12004' "$CFG" 2>/dev/null)
: "${_VLM_PORT:=12004}"

# Additional values used only as Spring Boot env overrides
_ARTEMIS_URL=$(yq '.broker.url // "tcp://localhost:12008"' "$CFG" 2>/dev/null)
: "${_ARTEMIS_URL:=tcp://localhost:12008}"
_COMFYUI_WS=$(yq '.broker.comfyui_ws // "ws://127.0.0.1:12006/ws"' "$CFG" 2>/dev/null)
: "${_COMFYUI_WS:=ws://127.0.0.1:12006/ws}"
_PG_URL=$(yq '.database.postgres_url // "jdbc:postgresql://localhost:12007/pipeline"' "$CFG" 2>/dev/null)
: "${_PG_URL:=jdbc:postgresql://localhost:12007/pipeline}"
_PG_USER=$(yq '.database.username // "pipeline"' "$CFG" 2>/dev/null)
: "${_PG_USER:=pipeline}"
_PG_PASS=$(yq '.database.password // "pipeline"' "$CFG" 2>/dev/null)
: "${_PG_PASS:=pipeline}"
_TACTICAL_MODEL=$(yq '.tactical.model.model_name // "default"' "$CFG" 2>/dev/null)
: "${_TACTICAL_MODEL:=default}"
_TACTICAL_TEMP=$(yq '.tactical.model.temperature // 0.1' "$CFG" 2>/dev/null)
: "${_TACTICAL_TEMP:=0.1}"
_TACTICAL_MAX_TOKENS=$(yq '.tactical.model.max_tokens // 128' "$CFG" 2>/dev/null)
: "${_TACTICAL_MAX_TOKENS:=128}"
_TACTICAL_MAX_TOKENS_THINKING=$(yq '.tactical.model.max_tokens_thinking // 512' "$CFG" 2>/dev/null)
: "${_TACTICAL_MAX_TOKENS_THINKING:=512}"
_TACTICAL_MAX_RETRIES=$(yq '.tactical.decisions.max_retries // 3' "$CFG" 2>/dev/null)
: "${_TACTICAL_MAX_RETRIES:=3}"
_TACTICAL_MAX_INPAINTS=$(yq '.tactical.decisions.max_inpaints // 2' "$CFG" 2>/dev/null)
: "${_TACTICAL_MAX_INPAINTS:=2}"
_TACTICAL_ACCEPT_VLM_MEAN=$(yq '.tactical.decisions.accept_vlm_mean_min // 7.0' "$CFG" 2>/dev/null)
: "${_TACTICAL_ACCEPT_VLM_MEAN:=7.0}"
_TACTICAL_ACCEPT_CLIP=$(yq '.tactical.decisions.accept_clip_min // 0.30' "$CFG" 2>/dev/null)
: "${_TACTICAL_ACCEPT_CLIP:=0.30}"
_STRATEGIC_URL=$(yq '.strategic.model.server_url // "http://localhost:12005/v1"' "$CFG" 2>/dev/null)
: "${_STRATEGIC_URL:=http://localhost:12005/v1}"
_STRATEGIC_MODEL=$(yq '.strategic.model.model_name // "default"' "$CFG" 2>/dev/null)
: "${_STRATEGIC_MODEL:=default}"
_CLIP_THRESHOLD=$(yq '.thresholds.clip // 0.25' "$CFG" 2>/dev/null)
: "${_CLIP_THRESHOLD:=0.25}"
_ARTIFACT_THRESHOLD=$(yq '.thresholds.artifact // 0.50' "$CFG" 2>/dev/null)
: "${_ARTIFACT_THRESHOLD:=0.50}"

# ── Spring Boot environment overrides ─────────────────────────────────────────
# Exported so the Java pipeline inherits config.yaml values at startup.
# Spring's relaxed binding maps UPPER_SNAKE_CASE to the equivalent property name.
export SPRING_ARTEMIS_BROKER_URL="$_ARTEMIS_URL"
export SPRING_DATASOURCE_URL="$_PG_URL"
export SPRING_DATASOURCE_USERNAME="$_PG_USER"
export SPRING_DATASOURCE_PASSWORD="$_PG_PASS"
export COMFYUI_URL="$_COMFYUI_URL"
export COMFYUI_WS="$_COMFYUI_WS"
export TACTICAL_LLM_URL="$_LLM_URL"
export TACTICAL_LLM_MODEL="$_TACTICAL_MODEL"
export TACTICAL_LLM_TEMPERATURE="$_TACTICAL_TEMP"
export TACTICAL_LLM_MAX_TOKENS="$_TACTICAL_MAX_TOKENS"
export TACTICAL_LLM_MAX_TOKENS_THINKING="$_TACTICAL_MAX_TOKENS_THINKING"
export TACTICAL_LLM_DECISIONS_MAX_RETRIES="$_TACTICAL_MAX_RETRIES"
export TACTICAL_LLM_DECISIONS_MAX_INPAINTS="$_TACTICAL_MAX_INPAINTS"
export TACTICAL_LLM_DECISIONS_ACCEPT_VLM_MEAN_MIN="$_TACTICAL_ACCEPT_VLM_MEAN"
export TACTICAL_LLM_DECISIONS_ACCEPT_CLIP_MIN="$_TACTICAL_ACCEPT_CLIP"
export STRATEGIC_LLM_URL="$_STRATEGIC_URL"
export STRATEGIC_LLM_MODEL="$_STRATEGIC_MODEL"
export SIDECARS_CLIP_SCORER_URL="http://localhost:${_CLIP_PORT}"
export SIDECARS_ARTIFACT_SCORER_URL="http://localhost:${_ARTIFACT_PORT}"
export SIDECARS_VLM_SCORER_URL="http://localhost:${_VLM_PORT}"
export SCORING_CLIP_THRESHOLD="$_CLIP_THRESHOLD"
export SCORING_ARTIFACT_THRESHOLD="$_ARTIFACT_THRESHOLD"

_PIPELINE_HEALTH="http://localhost:12000/actuator/health"

# ── Start ──────────────────────────────────────────────────────────────────────

# ── Escalation monitor ────────────────────────────────────────────────────────
# Forked by _start when --auto-escalate is passed. Watches the pipeline PID;
# when it exits, checks pipeline_events for a pending escalation and hands off
# to strategic.sh if one is found.
_monitor_escalation() {
    local pid_file="$PIDDIR/pipeline.pid"
    local pipeline_pid

    # Wait for the PID file to appear (pipeline.sh start is async)
    local waited=0
    while [ ! -f "$pid_file" ] && [ "$waited" -lt 30 ]; do
        sleep 1
        waited=$((waited + 1))
    done
    pipeline_pid=$(cat "$pid_file" 2>/dev/null) || return

    # Wait for the pipeline process to exit
    while kill -0 "$pipeline_pid" 2>/dev/null; do
        sleep 5
    done

    # Parse connection details from JDBC URL
    # jdbc:postgresql://HOST:PORT/DBNAME
    local pg_host pg_port pg_db
    pg_host=$(printf '%s' "$_PG_URL" | sed 's|jdbc:postgresql://\([^:]*\).*|\1|')
    pg_port=$(printf '%s' "$_PG_URL" | sed 's|jdbc:postgresql://[^:]*:\([0-9]*\).*|\1|')
    pg_db=$(printf '%s' "$_PG_URL" | sed 's|jdbc:postgresql://[^/]*/\([^?]*\).*|\1|')

    local pending
    pending=$(PGPASSWORD="$_PG_PASS" psql \
        -h "$pg_host" -p "$pg_port" -U "$_PG_USER" -d "$pg_db" \
        -tAc "SELECT id FROM pipeline_events WHERE type='mode_switch_requested' AND handled_at IS NULL ORDER BY created_at DESC LIMIT 1" \
        2>/dev/null)

    if [ -n "$pending" ]; then
        echo ""
        echo "==> Escalation detected (pipeline_events id=$pending). Starting strategic..."
        "$SOXHLET_ROOT/strategic.sh" start
    else
        echo ""
        echo "==> Pipeline exited with no pending escalation. Loop stopped."
    fi
}

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
    echo "==> Starting ComfyUI, tactical LLM, and ML sidecars..."
    "$LOOP_DIR/comfyui.sh" start
    "$LOOP_DIR/tactical_llm.sh" start
    "$LOOP_DIR/clip_scorer.sh" start
    "$LOOP_DIR/artifact_scorer.sh" start
    "$LOOP_DIR/vlm_scorer.sh" start

    component_wait_healthy "${_COMFYUI_URL}/system_stats"             "ComfyUI"         180      & _w1=$!
    component_wait_healthy "${_LLM_BASE}/v1/models"                   "tactical_llm"    300      & _w2=$!
    component_wait_healthy "http://localhost:${_CLIP_PORT}/health"     "clip-scorer"      60      & _w3=$!
    component_wait_healthy "http://localhost:${_ARTIFACT_PORT}/health" "artifact-scorer"  60      & _w4=$!
    component_wait_healthy "http://localhost:${_VLM_PORT}/health"      "vlm-scorer"      120      & _w5=$!

    _start_ok=0
    wait $_w1 || _start_ok=1
    wait $_w2 || _start_ok=1
    wait $_w3 || _start_ok=1
    wait $_w4 || _start_ok=1
    wait $_w5 || _start_ok=1
    [ "$_start_ok" -eq 1 ] && exit 1

    echo ""
    echo "==> Starting Java pipeline..."
    "$LOOP_DIR/pipeline.sh" start
    component_wait_healthy "$_PIPELINE_HEALTH" "pipeline" 120 '"UP"'

    echo ""
    _status

    if [ "${_AUTO_ESCALATE:-0}" = "1" ]; then
        echo "Auto-escalate enabled: monitoring pipeline for escalation handoff..."
        _monitor_escalation &
    fi
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
    echo "UI:   http://localhost:12000"
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
    start)
        case "${2:-}" in
            --auto-escalate) _AUTO_ESCALATE=1 _start ;;
            "")              _start ;;
            *) echo "Usage: $0 start [--auto-escalate]" >&2; exit 1 ;;
        esac ;;
    stop)
        case "${2:-}" in
            --all) _stop_all ;;
            *)     _stop ;;
        esac ;;
    restart) _stop; echo ""; _AUTO_ESCALATE=0 _start ;;
    status)  _status ;;
    health)  _health ;;
    *)
        echo "Usage: $0 {start|stop [--all]|restart|status|health}" >&2
        exit 1 ;;
esac
