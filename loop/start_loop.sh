#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
CFG="$AI_IMAGE_ROOT/config.yaml"

LLM_SERVER_URL=$(yq '.tactical.model.server_url // "http://localhost:8080/v1"' "$CFG" 2>/dev/null)
: "${LLM_SERVER_URL:=http://localhost:8080/v1}"

# Clear PID directory for a clean start.
rm -rf /tmp/ai-loop
mkdir -p /tmp/ai-loop

# K3s-managed services (Artemis + PostgreSQL) come up first.
"$LOOP_DIR/broker.sh" start

# ComfyUI and llama.cpp start in parallel — both are slow to initialise.
"$LOOP_DIR/comfyui.sh" start &
"$LOOP_DIR/llama_cpp.sh" start &
sleep 10

# Python ML sidecars (stateless FastAPI HTTP).
"$LOOP_DIR/clip_scorer.sh" start
"$LOOP_DIR/artifact_scorer.sh" start
"$LOOP_DIR/vlm_scorer.sh" start

# Java pipeline application (JMS workers + REST API + WebSocket).
"$LOOP_DIR/pipeline.sh" start

_HOST=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || hostname 2>/dev/null || echo "localhost")

echo ""
echo "Loop infrastructure ready"
echo ""
echo "K3s-managed services:"
echo "  PostgreSQL:  ${_HOST}:5432  (pipeline / pipeline)"
echo "  Artemis:     AMQP ${_HOST}:30672"
echo "  Artemis UI:  http://${_HOST}:30161  (admin / admin)"
echo ""
echo "Queue topology:"
echo "  loop.generate   [queue]  → ComfyUI worker (Java)"
echo "  loop.generated  [queue]  → Scorer (Java)"
echo "  loop.verdicts   [queue]  → Tactical LLM caller (Java)"
echo "  loop.retry      [queue]  → ComfyUI worker (Java)"
echo "  loop.inpaint    [queue]  → ComfyUI worker (Java)"
echo "  pipeline.dlx    [queue]  ← dead letters (Artemis console)"
echo ""
echo "Native services:"
echo "  Pipeline UI:   http://localhost:8090"
echo "  LLM server:    ${LLM_SERVER_URL}"
