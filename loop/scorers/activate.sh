#!/bin/bash
AI_IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CFG="$AI_IMAGE_ROOT/config.yaml"
SCORERS=$(yq '.paths.scorers' "$CFG")
cd "$SCORERS"
source venv/bin/activate
