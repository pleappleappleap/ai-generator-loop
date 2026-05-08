#!/bin/sh
CONTAINER="artemis-pipeline"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found." >&2
    exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "artemis: not created (run loop/start_broker.sh)"
    exit 0
fi

RUNNING=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null)
if [ "$RUNNING" = "true" ]; then
    _HOST=$(hostname 2>/dev/null || echo "localhost")
    echo "artemis: running"
    echo "  console: http://${_HOST}:8161  (admin / admin)"
    echo "  AMQP:    ${_HOST}:5672"
    echo "  STOMP:   ${_HOST}:61613"
else
    STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
    echo "artemis: stopped (${STATUS})"
fi
