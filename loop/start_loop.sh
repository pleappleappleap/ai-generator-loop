#!/bin/bash
AI_IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$AI_IMAGE_ROOT/config.yaml"
export AI_IMAGE_ROOT

COMFYUI=$(yq '.paths.comfyui' "$CFG")
SCORERS=$(yq '.paths.scorers' "$CFG")
LANCEDB=$(yq '.paths.lancedb' "$CFG")

# Start infrastructure
"$AI_IMAGE_ROOT/loop/start_broker.sh"
sleep 3

# Start ComfyUI
"$COMFYUI/launch.sh" &
sleep 5

# Start ComfyUI MQ worker
cd "$COMFYUI"
source venv/bin/activate
python "$AI_IMAGE_ROOT/loop/comfyui_worker.py" &

# Start Rust binaries
"$SCORERS/target/release/coordinator" &
sleep 1   # coordinator must bind its Unix socket before other processes start
"$SCORERS/target/release/router" &
"$SCORERS/target/release/aggregator" &
"$SCORERS/target/release/lancedb_manager" &

# Start Python scorers
cd "$SCORERS"
source venv/bin/activate
python clip_scorer.py &
python artifact_scorer.py &
python vlm_scorer.py &

# Start tactical LLM (shares the scorers venv)
cd "$AI_IMAGE_ROOT/tactical-llm"
python tactical_llm.py &

echo "Loop infrastructure ready"
echo ""
echo "Queue and exchange topology:"
echo "  loop.request                [queue]  → comfyui_worker"
echo "  loop.events                 [topic]  → router"
echo "  scorer.requests             [topic]  → all scorers (parallel)"
echo "  scorer.events               [topic]  → all scorers (cancel)"
echo "  scorer.results              [topic]"
echo "    clip.*                             → aggregator.clip.queue"
echo "                                       → lancedb.clip.queue"
echo "    artifact.*                         → aggregator.artifact.queue"
echo "                                       → lancedb.artifact.queue"
echo "    vlm.*                              → aggregator.vlm.queue"
echo "                                       → lancedb.vlm.queue"
echo "  scorer.result               [queue]  → tactical LLM"
echo "  loop.accepted               [topic]  → lancedb.accepted.queue"
echo ""
echo "  Redis keys:"
echo "    agg:session:<uuid>    aggregator correlation state"
echo "    ldb:session:<uuid>    lancedb manager correlation state"
echo ""
echo "  LanceDB: $LANCEDB"
echo "    tables: sessions, loop"
echo ""
echo "Management UI: http://localhost:8161 (Hawtio — admin/admin)"
