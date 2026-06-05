#!/bin/sh
# Soxhlet top-level manager.
# Single entry point for operating the full system.
# Middleware (Artemis + PostgreSQL + SearXNG) is always managed here.
# Exactly one of loop or strategic mode runs at a time.
#
# Usage:
#   soxhlet.sh start [loop | strategic]
#               Start middleware (if needed) then the requested mode.
#               Defaults to loop. If the other mode is running, stops it first.
#               No-op if already in the requested mode.
#               Loop mode always monitors for escalation and hands off to
#               strategic automatically when the pipeline signals it.
#   soxhlet.sh stop [--all]
#               Stop the active mode. With --all, also stop middleware.
#   soxhlet.sh restart
#               Restart middleware and the currently active mode.
#               Errors if nothing is running.
#   soxhlet.sh status
#               Show status of middleware and the active mode.
#   soxhlet.sh health
#               Health-check middleware and the active mode. Exits 0 if all healthy.

SOXHLET_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOOP_DIR="$SOXHLET_ROOT/loop"
export SOXHLET_ROOT
. "$LOOP_DIR/lib/component.sh"

# ── Mode detection ─────────────────────────────────────────────────────────────

_mode_script() {
    case "$1" in
        loop)      echo "$SOXHLET_ROOT/loop/loop.sh" ;;
        strategic) echo "$SOXHLET_ROOT/strategic-llm/strategic.sh" ;;
    esac
}

_detect_mode() {
    local pid
    if [ -f "$PIDDIR/strategic_llm.pid" ]; then
        pid=$(cat "$PIDDIR/strategic_llm.pid")
        kill -0 "$pid" 2>/dev/null && echo "strategic" && return
    fi
    if [ -f "$PIDDIR/comfyui.pid" ]; then
        pid=$(cat "$PIDDIR/comfyui.pid")
        kill -0 "$pid" 2>/dev/null && echo "loop" && return
    fi
    echo "none"
}

# ── Commands ───────────────────────────────────────────────────────────────────

_start() {
    local mode="${1:-loop}"
    case "$mode" in
        loop|strategic) ;;
        *) echo "Usage: $0 start [loop | strategic]" >&2; exit 1 ;;
    esac

    local current
    current=$(_detect_mode)

    if [ "$current" = "$mode" ]; then
        echo "==> Already in $mode mode — nothing to do."
        return 0
    fi

    if [ "$current" != "none" ]; then
        echo "==> Stopping $current mode..."
        "$(_mode_script "$current")" stop
        echo ""
    fi

    "$(_mode_script "$mode")" start
}

_stop() {
    local current
    current=$(_detect_mode)
    if [ "$current" = "none" ]; then
        echo "==> Nothing is running."
    else
        "$(_mode_script "$current")" stop
    fi
}

_stop_all() {
    _stop
    echo ""
    "$SOXHLET_ROOT/middleware.sh" stop --all
}

_restart() {
    local current
    current=$(_detect_mode)
    if [ "$current" = "none" ]; then
        echo "ERROR: Nothing is running — use '$0 start' to start." >&2
        exit 1
    fi
    "$(_mode_script "$current")" restart
}

_status() {
    local current
    current=$(_detect_mode)
    "$(_mode_script "${current:-loop}")" status 2>/dev/null || "$SOXHLET_ROOT/middleware.sh" status
}

_health() {
    local current ok=0
    current=$(_detect_mode)
    "$SOXHLET_ROOT/middleware.sh" health || ok=1
    case "$current" in
        loop|strategic) "$(_mode_script "$current")" health || ok=1 ;;
        none) echo "No mode is running."; ok=1 ;;
    esac
    return $ok
}

# ── Dispatch ───────────────────────────────────────────────────────────────────

case "${1:-status}" in
    start)  _start "${2:-loop}" ;;
    stop)
        case "${2:-}" in
            --all) _stop_all ;;
            "")    _stop ;;
            *) echo "Usage: $0 stop [--all]" >&2; exit 1 ;;
        esac ;;
    restart) _restart ;;
    status)  _status ;;
    health)  _health ;;
    *)
        echo "Usage: $0 {start [loop|strategic] | stop [--all] | restart | status | health}" >&2
        exit 1 ;;
esac
