#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
CFG="$AI_IMAGE_ROOT/config.yaml"

LLM_SERVER_URL=$(yq '.tactical.model.server_url // "http://localhost:8080/v1"' "$CFG" 2>/dev/null)
: "${LLM_SERVER_URL:=http://localhost:8080/v1}"
UI_PORT=$(yq '.ui.port // 7860' "$CFG" 2>/dev/null)
: "${UI_PORT:=7860}"

# Clear PID directory for a clean start.
rm -rf /tmp/ai-loop
mkdir -p /tmp/ai-loop

# K3s-managed services (Artemis + PostgreSQL) come up first.
"$LOOP_DIR/broker.sh" start

# ComfyUI and llama.cpp start in parallel — both are slow to initialise.
"$LOOP_DIR/comfyui.sh" start &
"$LOOP_DIR/llama_cpp.sh" start &
sleep 10

# Coordinator first — other processes connect to its Unix socket.
"$LOOP_DIR/coordinator.sh" start
sleep 1

"$LOOP_DIR/comfyui_worker.sh" start
"$LOOP_DIR/router.sh" start
"$LOOP_DIR/aggregator.sh" start
"$LOOP_DIR/lancedb_manager.sh" start
"$LOOP_DIR/clip_scorer.sh" start
"$LOOP_DIR/artifact_scorer.sh" start
"$LOOP_DIR/vlm_scorer.sh" start
"$LOOP_DIR/tactical_llm.sh" start
"$LOOP_DIR/ui_server.sh" start
"$LOOP_DIR/monitor.sh" start

_HOST=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || hostname 2>/dev/null || echo "localhost")

echo ""
echo "Loop infrastructure ready"
echo ""
echo "K3s-managed services:"
echo "  PostgreSQL:  ${_HOST}:5432  (pipeline / pipeline)"
echo "  Artemis:     AMQP ${_HOST}:30672  STOMP ${_HOST}:30613"
echo "  Artemis UI:  http://${_HOST}:30161  (admin / admin)"
echo ""
echo "Queue topology (current — pre-Java migration):"
echo "  loop.request                [queue]  → comfyui_worker"
echo "  loop.events                 [topic]  → router"
echo "  scorer.requests             [topic]  → all scorers (parallel)"
echo "  scorer.events               [topic]  → all scorers (cancel)"
echo "  aggregator.clip.queue       [queue]  ← clip_scorer"
echo "  aggregator.artifact.queue   [queue]  ← artifact_scorer"
echo "  aggregator.vlm.queue        [queue]  ← vlm_scorer"
echo "  scorer.result               [queue]  → tactical LLM"
echo "  pipeline.dead               [queue]  ← pipeline.dlx (dead letters) → monitor"
echo ""
echo "Native services:"
echo "  Pipeline UI:  http://localhost:${UI_PORT}"
echo "  LLM server:   ${LLM_SERVER_URL}"
