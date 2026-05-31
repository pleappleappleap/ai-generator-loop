#!/bin/sh
# Shared component lifecycle library.  Source this file; do not execute directly.
#
# Requires SOXHLET_ROOT to be set before sourcing.
# Provides: component_start, component_stop, component_restart, component_status
#
# Usage in component scripts:
#   LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
#   SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$LOOP_DIR")}"
#   export SOXHLET_ROOT
#   . "$LOOP_DIR/lib/component.sh"

PIDDIR="${PIDDIR:-/tmp/ai-loop}"
CFG="$SOXHLET_ROOT/config.yaml"
SCORERS="$SOXHLET_ROOT/loop/scorers"
COMFYUI_DIR="$SOXHLET_ROOT/loop/ComfyUI"
VENV="$SOXHLET_ROOT/venv"
SVENV="$SCORERS/venv"

# Resolve yq "auto" paths using the config file.
_yq_resolve() {
    local val
    val=$(yq "$1" "$CFG" 2>/dev/null)
    echo "${val:-}"
}

component_status() {
    local name="$1" pid pid_file="$PIDDIR/$1.pid"
    if [ ! -f "$pid_file" ]; then
        printf "%-22s %s\n" "$name" "not started"
        return 1
    fi
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        printf "%-22s %s\n" "$name" "running (PID $pid)"
        return 0
    else
        printf "%-22s %s\n" "$name" "dead (PID $pid)"
        return 1
    fi
}

component_stop() {
    local name="$1" pid pid_file="$PIDDIR/$1.pid"
    if [ ! -f "$pid_file" ]; then
        echo "==> $name: not running"
        return 0
    fi
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        pkill -P "$pid" 2>/dev/null
        kill "$pid" 2>/dev/null
        echo "==> $name: stopped (PID $pid)"
    else
        echo "==> $name: already stopped"
    fi
    rm -f "$pid_file"
}

component_start() {
    local name="$1" pid pid_file="$PIDDIR/$1.pid"
    shift
    if [ -f "$pid_file" ]; then
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "==> $name: already running (PID $pid)"
            return 0
        fi
        rm -f "$pid_file"
    fi
    mkdir -p "$PIDDIR"
    local logfile="$PIDDIR/$name.log"
    "$@" >> "$logfile" 2>&1 &
    echo $! > "$pid_file"
    echo "==> $name: started (PID $(cat "$pid_file")) log=$logfile"
}

component_restart() {
    local name="$1"
    shift
    component_stop "$name"
    sleep 1
    component_start "$name" "$@"
}

# component_check_healthy url [match]
# Returns 0 if url responds HTTP 200 (and body contains match if given), 1 otherwise.
component_check_healthy() {
    local url="$1" match="${2:-}"
    local body
    body=$(curl -sf --max-time 3 "$url" 2>/dev/null) || return 1
    [ -z "$match" ] || echo "$body" | grep -q "$match" || return 1
    return 0
}

# component_wait_healthy url name [max_wait=120] [match]
# Blocks until healthy or exits the script with an error.
component_wait_healthy() {
    local url="$1" name="$2" max_wait="${3:-120}" match="${4:-}"
    local interval=2 elapsed=0
    printf "==> Waiting for %s" "$name"
    while ! component_check_healthy "$url" "$match"; do
        if [ "$elapsed" -ge "$max_wait" ]; then
            echo ""
            echo "ERROR: $name did not become healthy within ${max_wait}s — check $(basename "$PIDDIR")/$name.log" >&2
            exit 1
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
        printf "."
    done
    echo " ready (${elapsed}s)"
}
