#!/bin/sh
PIDDIR="/tmp/ai-loop"

printf "%-22s %s\n" "PROCESS" "STATUS"
printf "%-22s %s\n" "-------" "------"

for name in comfyui llama_cpp coordinator comfyui_worker router aggregator \
            lancedb_manager clip_scorer artifact_scorer vlm_scorer \
            tactical_llm ui_server monitor; do
    pidfile="$PIDDIR/${name}.pid"
    if [ ! -f "$pidfile" ]; then
        printf "%-22s %s\n" "$name" "not started"
        continue
    fi
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
        printf "%-22s %s\n" "$name" "running (PID $pid)"
    else
        printf "%-22s %s\n" "$name" "dead (PID $pid)"
    fi
done

echo ""
CONTAINER="artemis-pipeline"
if command -v docker >/dev/null 2>&1 && docker inspect "$CONTAINER" >/dev/null 2>&1; then
    RUNNING=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null)
    if [ "$RUNNING" = "true" ]; then
        printf "%-22s %s\n" "artemis" "running"
    else
        printf "%-22s %s\n" "artemis" "stopped"
    fi
else
    printf "%-22s %s\n" "artemis" "not found"
fi
