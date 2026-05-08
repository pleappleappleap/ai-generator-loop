#!/bin/sh
CONTAINER="artemis-pipeline"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "==> Artemis container '$CONTAINER' does not exist"
    exit 0
fi

if docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    docker stop "$CONTAINER"
    echo "==> Artemis stopped"
else
    echo "==> Artemis already stopped"
fi
