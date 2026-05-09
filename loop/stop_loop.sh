#!/bin/sh
# Stops all loop processes. Does not stop the broker — use broker.sh stop for that.
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT

for comp in monitor ui_server tactical_llm vlm_scorer artifact_scorer clip_scorer \
            lancedb_manager aggregator router comfyui_worker coordinator llama_cpp comfyui; do
    "$LOOP_DIR/${comp}.sh" stop
done

echo "==> Loop stopped"
