#!/bin/sh
# Shared component lifecycle library.  Source this file; do not execute directly.
#
# Requires AI_IMAGE_ROOT to be set before sourcing.
# Provides: component_start, component_stop, component_restart, component_status
#
# Usage in component scripts:
#   LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
#   AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
#   export AI_IMAGE_ROOT
#   . "$LOOP_DIR/lib/component.sh"

PIDDIR="${PIDDIR:-/tmp/ai-loop}"
CFG="$AI_IMAGE_ROOT/config.yaml"
SCORERS="$AI_IMAGE_ROOT/loop/scorers"
COMFYUI_DIR="$AI_IMAGE_ROOT/loop/ComfyUI"
VENV="$AI_IMAGE_ROOT/venv"
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
