#!/bin/sh
# Stops all loop processes started by start_loop.sh.
# Does not stop the broker — use stop_broker.sh for that.

PIDDIR="/tmp/ai-loop"

for pidfile in "$PIDDIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    pid=$(cat "$pidfile")
    name=$(basename "$pidfile" .pid)
    if kill -0 "$pid" 2>/dev/null; then
        echo "==> Stopping $name (PID $pid)"
        pkill -P "$pid" 2>/dev/null   # kill children (e.g. ComfyUI python under launch.sh shell)
        kill "$pid" 2>/dev/null
    fi
    rm -f "$pidfile"
done

echo "==> Loop stopped"
