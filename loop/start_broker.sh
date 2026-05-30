#!/bin/sh
# Starts ActiveMQ Artemis via Docker.
#
# First run: pulls the image and creates the container with AMQP (5672),
# STOMP (61613), and the management console (8161) exposed.
# Subsequent runs: restarts the existing stopped container.
#
# Broker data is persisted in loop/artemis-data/ (bind mount).
AI_IMAGE_ROOT="${1:-${AI_IMAGE_ROOT:-}}"
if [ -z "$AI_IMAGE_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    AI_IMAGE_ROOT="$PWD"
fi
: "${AI_IMAGE_ROOT:?provide root as \$1, set AI_IMAGE_ROOT in environment, or run from the repo root}"

CONTAINER="artemis-pipeline"
DATA_DIR="$AI_IMAGE_ROOT/loop/artemis-data"
mkdir -p "$DATA_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found. Install Docker Desktop or Docker Engine first." >&2
    exit 1
fi

# If running, nothing to do.
if docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    echo "==> Artemis already running (container: $CONTAINER)"
    _HOST=$(hostname 2>/dev/null || echo "localhost")
    echo "==> Artemis console: http://${_HOST}:8161  (admin / admin)"
    exit 0
fi

# If container exists but is stopped, restart it.
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker start "$CONTAINER"
else
    # First run: create the container.
    docker run -d \
        --name "$CONTAINER" \
        -p 61616:61616 \
        -p 5672:5672 \
        -p 61613:61613 \
        -p 8161:8161 \
        -e ARTEMIS_USER=admin \
        -e ARTEMIS_PASSWORD=admin \
        -v "$DATA_DIR:/var/lib/artemis-instance" \
        apache/activemq-artemis:latest
fi

_HOST=$(hostname 2>/dev/null || echo "localhost")
echo "==> Artemis console: http://${_HOST}:8161  (admin / admin)"
