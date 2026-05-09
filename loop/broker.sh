#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
CONTAINER="artemis-pipeline"

_status() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: docker not found." >&2; return 1
    fi
    if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
        printf "%-22s %s\n" "artemis" "not created"; return 1
    fi
    RUNNING=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null)
    if [ "$RUNNING" = "true" ]; then
        _HOST=$(hostname 2>/dev/null || echo "localhost")
        printf "%-22s %s\n" "artemis" "running"
        echo "  console: http://${_HOST}:8161  (admin / admin)"
        echo "  AMQP:    ${_HOST}:5672"
        echo "  STOMP:   ${_HOST}:61613"
        return 0
    fi
    STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
    printf "%-22s %s\n" "artemis" "stopped (${STATUS})"; return 1
}

case "${1:-status}" in
    start)   "$LOOP_DIR/start_broker.sh" ;;
    stop)    "$LOOP_DIR/stop_broker.sh" ;;
    restart) docker restart "$CONTAINER" && echo "==> artemis: restarted" ;;
    status)  _status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
