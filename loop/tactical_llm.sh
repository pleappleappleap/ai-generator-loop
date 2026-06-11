#!/bin/sh
# Tactical LLM component manager.
#
# macOS:          mlx_lm.server (MLX native, loads MLX model directory)
# Linux/Windows:  llama_cpp.server (GGUF, CUDA or CPU)
#
# Both backends expose an OpenAI-compatible API.
# Health check: GET /v1/models (neither server has a /health endpoint).
#
# Usage: tactical_llm.sh {start|stop|restart|status|health}

LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$LOOP_DIR")}"
export SOXHLET_ROOT
. "$LOOP_DIR/lib/component.sh"

_resolve() {
    LLM_SERVER_URL=$(yq '.tactical.model.server_url // "http://localhost:12001/v1"' "$CFG" 2>/dev/null)
    : "${LLM_SERVER_URL:=http://localhost:12001/v1}"
    LLM_PORT=$(echo "$LLM_SERVER_URL" | sed 's|.*://[^:/]*:||; s|/.*||')
    : "${LLM_PORT:=12001}"

    LLM_DIR=$(yq '.tactical.model.dir' "$CFG" 2>/dev/null)
    [ "$LLM_DIR" = "auto" ] || [ -z "$LLM_DIR" ] && LLM_DIR="$SCORERS/models/tactical"

    LLM_CTX=$(yq '.tactical.model.context_length // 8192' "$CFG" 2>/dev/null)
    : "${LLM_CTX:=8192}"

    if [ "$(uname -s)" = "Darwin" ]; then
        LLM_NAME=$(yq '.tactical.model.name' "$CFG" 2>/dev/null)
        : "${LLM_NAME:=}"
        LLM_BACKEND=mlx
    else
        LLM_NAME=$(yq '.tactical.model.linux.name' "$CFG" 2>/dev/null)
        : "${LLM_NAME:=}"
        LLM_BACKEND=llama_cpp
    fi

    LLM_MODEL="$LLM_DIR/$LLM_NAME"
}

_start() {
    _resolve
    if [ "$LLM_BACKEND" = "mlx" ]; then
        if [ ! -d "$LLM_MODEL" ]; then
            echo "==> tactical_llm: MLX model not found at $LLM_MODEL — run 'make models'" >&2
            return 1
        fi
        component_start tactical_llm \
            "$SVENV/bin/python" -m mlx_lm.server \
            --model "$LLM_MODEL" \
            --port "$LLM_PORT" \
            --host 0.0.0.0 \
            --chat-template-args '{"enable_thinking":false}'
    else
        if [ ! -f "$LLM_MODEL" ]; then
            echo "==> tactical_llm: GGUF model not found at $LLM_MODEL — run 'make models'" >&2
            return 1
        fi
        component_start tactical_llm \
            "$SVENV/bin/python" -m llama_cpp.server \
            --model "$LLM_MODEL" \
            --port "$LLM_PORT" \
            --host 0.0.0.0 \
            --n_ctx "$LLM_CTX"
    fi
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
