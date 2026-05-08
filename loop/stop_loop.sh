#!/bin/sh
# Stops all loop processes started by start_loop.sh.
# Does not stop the broker — use stop_broker.sh for that.

stop_by_match() {
    pattern="$1"
    pids=$(pgrep -f "$pattern" 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "==> Stopping: $pattern"
        kill $pids 2>/dev/null
    fi
}

stop_by_match "coordinator"
stop_by_match "router"
stop_by_match "aggregator"
stop_by_match "lancedb_manager"
stop_by_match "comfyui_worker.py"
stop_by_match "clip_scorer.py"
stop_by_match "artifact_scorer.py"
stop_by_match "vlm_scorer.py"
stop_by_match "tactical_llm.py"
stop_by_match "loop/ui/server.py"
stop_by_match "loop/monitor.py"
stop_by_match "llama_cpp.server"
stop_by_match "ComfyUI/venv/bin/python"

echo "==> Loop stopped"
