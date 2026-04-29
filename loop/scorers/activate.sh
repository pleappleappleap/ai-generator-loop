#!/bin/sh
AI_IMAGE_ROOT="${1:-${AI_IMAGE_ROOT:-}}"
if [ -z "$AI_IMAGE_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    AI_IMAGE_ROOT="$PWD"
fi
: "${AI_IMAGE_ROOT:?provide root as \$1, set AI_IMAGE_ROOT in environment, or run from the repo root}"
CFG="$AI_IMAGE_ROOT/config.yaml"
SCORERS=$(yq '.paths.scorers' "$CFG")
[ "$SCORERS" = "auto" ] && SCORERS="$AI_IMAGE_ROOT/loop/scorers"
cd "$SCORERS"
. venv/bin/activate
