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

# Start infrastructure
"$AI_IMAGE_ROOT/loop/start_broker.sh"
sleep 3

# Start ComfyUI
"$COMFYUI/launch.sh" &
sleep 5

# Start ComfyUI MQ worker (uses root venv; config.py is at AI_IMAGE_ROOT)
cd "$AI_IMAGE_ROOT"
. venv/bin/activate
python loop/comfyui_worker.py &

# Start Rust binaries
"$SCORERS/target/release/coordinator" &
sleep 1   # coordinator must bind its Unix socket before other processes start
"$SCORERS/target/release/router" &
"$SCORERS/target/release/aggregator" &
"$SCORERS/target/release/lancedb_manager" &

# Start Python scorers
cd "$SCORERS"
. venv/bin/activate
python clip_scorer.py &
python artifact_scorer.py &
python vlm_scorer.py &

# Start tactical LLM (shares the scorers venv; config.py lives at AI_IMAGE_ROOT)
cd "$AI_IMAGE_ROOT/tactical-llm"
PYTHONPATH="$AI_IMAGE_ROOT" python tactical_llm.py &

# Start dead-letter monitor (root venv; watches pipeline.dead for unrecoverable failures)
cd "$AI_IMAGE_ROOT"
python loop/monitor.py &

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
echo "Management UI: http://${_HOST}:8161 (Hawtio, admin/admin)"
