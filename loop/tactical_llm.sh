#!/bin/sh
# Tactical LLM component manager.
# Serves the tactical decision model via mlx_lm.server (OpenAI-compatible API).
# Health check: GET /v1/models (mlx_lm.server has no /health endpoint).
#
# Usage: tactical_llm.sh {start|stop|restart|status|health}

LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

_resolve() {
    LLM_SERVER_URL=$(yq '.tactical.model.server_url // "http://localhost:8080/v1"' "$CFG" 2>/dev/null)
    : "${LLM_SERVER_URL:=http://localhost:8080/v1}"
    # Extract port: strip scheme, host, and path leaving just the number.
    LLM_PORT=$(echo "$LLM_SERVER_URL" | sed 's|.*://[^:/]*:||; s|/.*||')
    : "${LLM_PORT:=8080}"

    LLM_DIR=$(yq '.tactical.model.dir' "$CFG" 2>/dev/null)
    [ "$LLM_DIR" = "auto" ] || [ -z "$LLM_DIR" ] && LLM_DIR="$SCORERS/models/tactical"
    LLM_NAME=$(yq '.tactical.model.name' "$CFG" 2>/dev/null)
    : "${LLM_NAME:=}"
    LLM_MODEL="$LLM_DIR/$LLM_NAME"

    LLM_CTX=$(yq '.tactical.model.context_length // 8192' "$CFG" 2>/dev/null)
    : "${LLM_CTX:=8192}"
}

_start() {
    _resolve
    if [ ! -d "$LLM_MODEL" ]; then
        echo "==> tactical_llm: model not found at $LLM_MODEL — run 'make models'" >&2
        return 1
    fi
    component_start tactical_llm \
        "$SVENV/bin/python" -m mlx_lm.server \
        --model "$LLM_MODEL" \
        --port "$LLM_PORT" \
        --host 0.0.0.0 \
        --max-tokens 128
}

case "${1:-status}" in
    start)   _start ;;
    stop)    component_stop tactical_llm ;;
    restart) component_stop tactical_llm; sleep 1; _start ;;
    status)  component_status tactical_llm ;;
    health)
        _resolve
        if component_check_healthy "http://localhost:${LLM_PORT}/v1/models"; then
            printf "%-22s %s\n" "tactical_llm" "healthy"
        else
            printf "%-22s %s\n" "tactical_llm" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
