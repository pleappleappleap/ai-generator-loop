#!/bin/sh
SOXHLET_ROOT="${1:-${SOXHLET_ROOT:-}}"
if [ -z "$SOXHLET_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    SOXHLET_ROOT="$PWD"
fi
: "${SOXHLET_ROOT:?provide root as \$1, set SOXHLET_ROOT in environment, or run from the repo root}"
CFG="$SOXHLET_ROOT/config.yaml"
SCORERS=$(yq '.paths.scorers' "$CFG")
[ "$SCORERS" = "auto" ] && SCORERS="$SOXHLET_ROOT/loop/scorers"
cd "$SCORERS"
. venv/bin/activate
