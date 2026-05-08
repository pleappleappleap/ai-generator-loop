#!/bin/sh
AI_IMAGE_ROOT="${1:-${AI_IMAGE_ROOT:-}}"
if [ -z "$AI_IMAGE_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    AI_IMAGE_ROOT="$PWD"
fi
: "${AI_IMAGE_ROOT:?provide root as \$1, set AI_IMAGE_ROOT in environment, or run from the repo root}"
CFG="$AI_IMAGE_ROOT/config.yaml"
export AI_IMAGE_ROOT

COMFYUI=$(yq '.paths.comfyui' "$CFG")
SCORERS=$(yq '.paths.scorers' "$CFG")
LANCEDB=$(yq '.paths.lancedb' "$CFG")

# Resolve "auto" path values (mirrors check_env.py _resolve logic)
[ "$COMFYUI" = "auto" ] && COMFYUI="$AI_IMAGE_ROOT/loop/ComfyUI"
[ "$SCORERS" = "auto" ] && SCORERS="$AI_IMAGE_ROOT/loop/scorers"
[ "$LANCEDB" = "auto" ] && LANCEDB="$AI_IMAGE_ROOT/lancedb"

# Resolve llama.cpp server config before any sleeps so we can start it early.
LLM_SERVER_URL=$("$AI_IMAGE_ROOT/venv/bin/python" -c "
import sys; sys.path.insert(0,'$AI_IMAGE_ROOT')
from config import load; cfg=load()
print(cfg.tactical.get('model',{}).get('server_url','http://localhost:8080/v1'))
" 2>/dev/null || echo "http://localhost:8080/v1")
LLM_PORT=$(echo "$LLM_SERVER_URL" | sed 's|.*:\([0-9]*\)/.*|\1|; s|http://[^:]*||')
[ -z "$LLM_PORT" ] && LLM_PORT=8080
LLM_MODEL_DIR=$("$AI_IMAGE_ROOT/venv/bin/python" -c "
import sys; sys.path.insert(0,'$AI_IMAGE_ROOT')
from config import load; cfg=load(); m=cfg.tactical.get('model',{})
print(m.get('dir','') + '/' + m.get('filename',''))
" 2>/dev/null || echo "")
LLM_GPU_LAYERS=$("$AI_IMAGE_ROOT/venv/bin/python" -c "
import sys; sys.path.insert(0,'$AI_IMAGE_ROOT')
from config import load; cfg=load()
print(cfg.compute.get('tactical_llm',{}).get('n_gpu_layers',-1))
" 2>/dev/null || echo "-1")

PIDDIR="/tmp/ai-loop"
rm -rf "$PIDDIR"
mkdir -p "$PIDDIR"

# Start Artemis, ComfyUI, and llama.cpp server in parallel — none depend on each other.
# All three are slow to initialise; overlapping them saves the most wall-clock time.
"$AI_IMAGE_ROOT/loop/start_broker.sh" &   # Docker-managed; no PID file needed
"$COMFYUI/launch.sh" &
echo $! > "$PIDDIR/comfyui.pid"
if [ -n "$LLM_MODEL_DIR" ] && [ -f "$LLM_MODEL_DIR" ]; then
    "$SCORERS/venv/bin/python" -m llama_cpp.server \
        --model "$LLM_MODEL_DIR" \
        --n_gpu_layers "$LLM_GPU_LAYERS" \
        --host 0.0.0.0 \
        --port "$LLM_PORT" &
    echo $! > "$PIDDIR/llama_cpp.pid"
    echo "llama.cpp server PID=$(cat "$PIDDIR/llama_cpp.pid") port=$LLM_PORT"
else
    echo "WARNING: LLM model not found at '$LLM_MODEL_DIR' — skipping llama.cpp server"
fi

# Wait for the slowest of the three to be ready.
# ComfyUI and the LLM both load large models; 10 s covers Artemis comfortably.
sleep 10

# Start coordinator first — other processes connect to its Unix socket.
"$SCORERS/target/release/coordinator" &
echo $! > "$PIDDIR/coordinator.pid"
sleep 1   # coordinator must bind its Unix socket before other processes start

# Start ComfyUI MQ worker (uses root venv; config.py is at AI_IMAGE_ROOT)
"$AI_IMAGE_ROOT/venv/bin/python" "$AI_IMAGE_ROOT/loop/comfyui_worker.py" &
echo $! > "$PIDDIR/comfyui_worker.pid"
"$SCORERS/target/release/router" &
echo $! > "$PIDDIR/router.pid"
"$SCORERS/target/release/aggregator" &
echo $! > "$PIDDIR/aggregator.pid"
"$SCORERS/target/release/lancedb_manager" &
echo $! > "$PIDDIR/lancedb_manager.pid"

# Start Python scorers
PYTHONPATH="$AI_IMAGE_ROOT" "$SCORERS/venv/bin/python" "$SCORERS/clip_scorer.py" &
echo $! > "$PIDDIR/clip_scorer.pid"
PYTHONPATH="$AI_IMAGE_ROOT" "$SCORERS/venv/bin/python" "$SCORERS/artifact_scorer.py" &
echo $! > "$PIDDIR/artifact_scorer.pid"
PYTHONPATH="$AI_IMAGE_ROOT" "$SCORERS/venv/bin/python" "$SCORERS/vlm_scorer.py" &
echo $! > "$PIDDIR/vlm_scorer.pid"

PYTHONPATH="$AI_IMAGE_ROOT" "$AI_IMAGE_ROOT/venv/bin/python" "$AI_IMAGE_ROOT/tactical-llm/tactical_llm.py" &
echo $! > "$PIDDIR/tactical_llm.pid"

# Start UI server
UI_PORT=$("$AI_IMAGE_ROOT/venv/bin/python" -c "
import sys; sys.path.insert(0,'$AI_IMAGE_ROOT')
from config import load; cfg=load()
print(cfg.get('ui', default={}).get('port', 7860) if cfg.get('ui') else 7860)
" 2>/dev/null || echo "7860")
PYTHONPATH="$AI_IMAGE_ROOT" "$AI_IMAGE_ROOT/venv/bin/python" "$AI_IMAGE_ROOT/loop/ui/server.py" &
echo $! > "$PIDDIR/ui_server.pid"

# Start dead-letter monitor (root venv; watches pipeline.dead for unrecoverable failures)
"$AI_IMAGE_ROOT/venv/bin/python" "$AI_IMAGE_ROOT/loop/monitor.py" &
echo $! > "$PIDDIR/monitor.pid"

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
