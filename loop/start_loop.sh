#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../config.sh
source "$SCRIPT_DIR/../config.sh"

# Start infrastructure
"$SCRIPT_DIR/start_broker.sh"
sleep 3

# Start ComfyUI
"$AI_IMAGE_COMFYUI/launch.sh" &
sleep 5

# Start ComfyUI MQ worker
cd "$AI_IMAGE_COMFYUI"
source venv/bin/activate
python "$SCRIPT_DIR/comfyui_worker.py" &

# Start Rust router
"$AI_IMAGE_SCORERS/router/target/release/router" &

# Start Python scorers and managers
cd "$AI_IMAGE_SCORERS"
source venv/bin/activate
python clip_scorer.py &
python artifact_scorer.py &
python vlm_scorer.py &
python scorer_aggregator.py &
python "$AI_IMAGE_ROOT/lancedb_manager.py" &

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
echo "  LanceDB: $AI_IMAGE_LANCEDB"
echo "    tables: sessions, loop"
echo ""
echo "Management UI: http://localhost:15672 (guest/guest)"
