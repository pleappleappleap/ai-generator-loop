#!/bin/sh
# Stops all native loop processes. K3s-managed services (Artemis, PostgreSQL)
# are left running — they are infrastructure, not process-lifecycle components.
# To stop Artemis: broker.sh stop
# To tear down everything: kubectl delete -k k8s/
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT

for comp in monitor ui_server tactical_llm vlm_scorer artifact_scorer clip_scorer \
            lancedb_manager aggregator router comfyui_worker coordinator llama_cpp comfyui; do
    "$LOOP_DIR/${comp}.sh" stop
done

echo "==> Loop stopped (K3s services still running)"
