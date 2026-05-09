#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
CFG="$AI_IMAGE_ROOT/config.yaml"

LANCEDB=$(yq '.paths.lancedb' "$CFG")
[ "$LANCEDB" = "auto" ] && LANCEDB="$AI_IMAGE_ROOT/lancedb"

LLM_SERVER_URL=$(yq '.tactical.model.server_url // "http://localhost:8080/v1"' "$CFG" 2>/dev/null)
: "${LLM_SERVER_URL:=http://localhost:8080/v1}"
UI_PORT=$(yq '.ui.port // 7860' "$CFG" 2>/dev/null)
: "${UI_PORT:=7860}"

# Clear PID directory for a clean start.
rm -rf /tmp/ai-loop
mkdir -p /tmp/ai-loop

# Artemis, ComfyUI, and llama.cpp start in parallel — all are slow to initialise.
"$LOOP_DIR/broker.sh" start &
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

echo ""
echo "Loop infrastructure ready"
echo ""
echo "Queue and exchange topology:"
echo "  loop.request                [queue]  → comfyui_worker"
echo "  loop.events                 [topic]  → router"
echo "  scorer.requests             [topic]  → all scorers (parallel)"
echo "  scorer.events               [topic]  → all scorers (cancel)"
echo "  aggregator.clip.queue       [queue]  ← clip_scorer"
echo "  aggregator.artifact.queue   [queue]  ← artifact_scorer"
echo "  aggregator.vlm.queue        [queue]  ← vlm_scorer"
echo "  scorer.result               [queue]  → tactical LLM"
echo "  lancedb.accepted.queue      [queue]  ← loop.accepted topic"
echo "  pipeline.dead               [queue]  ← pipeline.dlx (dead letters) → monitor"
echo ""
echo "  LanceDB: $LANCEDB"
echo "    tables: sessions, loop"
echo ""
_HOST=$(hostname 2>/dev/null || echo "localhost")
echo "Management UI:  http://${_HOST}:8161 (Hawtio, admin/admin)"
echo "Pipeline UI:    http://${_HOST}:${UI_PORT}"
echo "LLM server:     ${LLM_SERVER_URL}"
