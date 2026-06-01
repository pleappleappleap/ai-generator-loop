#!/bin/sh
# Strategic LLM component manager.
#
# macOS:          mlx_lm.server (MLX native, loads MLX model directory)
# Linux/Windows:  llama_cpp.server (GGUF, CUDA or CPU)
#
# Both backends expose an OpenAI-compatible API.
# Health check: GET /v1/models (neither server has a /health endpoint).
#
# Usage: strategic_llm.sh {start|stop|restart|status|health}

STRATEGIC_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$STRATEGIC_DIR")}"
export SOXHLET_ROOT
. "$SOXHLET_ROOT/loop/lib/component.sh"

_resolve() {
    LLM_SERVER_URL=$(yq '.strategic.model.server_url // "http://localhost:12005/v1"' "$CFG" 2>/dev/null)
    : "${LLM_SERVER_URL:=http://localhost:12005/v1}"
    LLM_PORT=$(echo "$LLM_SERVER_URL" | sed 's|.*://[^:/]*:||; s|/.*||')
    : "${LLM_PORT:=12005}"

    LLM_DIR=$(yq '.strategic.model.dir' "$CFG" 2>/dev/null)
    [ "$LLM_DIR" = "auto" ] || [ -z "$LLM_DIR" ] && LLM_DIR="$SCORERS/models/strategic"

    LLM_CTX=$(yq '.strategic.model.context_length // 32768' "$CFG" 2>/dev/null)
    : "${LLM_CTX:=32768}"

    if [ "$(uname -s)" = "Darwin" ]; then
        LLM_NAME=$(yq '.strategic.model.name' "$CFG" 2>/dev/null)
        : "${LLM_NAME:=}"
        LLM_BACKEND=mlx
    else
        LLM_NAME=$(yq '.strategic.model.linux.name' "$CFG" 2>/dev/null)
        : "${LLM_NAME:=}"
        LLM_BACKEND=llama_cpp
    fi

    LLM_MODEL="$LLM_DIR/$LLM_NAME"
}

_start() {
    _resolve
    if [ "$LLM_BACKEND" = "mlx" ]; then
        if [ ! -d "$LLM_MODEL" ]; then
            echo "==> strategic_llm: MLX model not found at $LLM_MODEL — run 'make models'" >&2
            return 1
        fi
        component_start strategic_llm \
            "$SVENV/bin/python" -m mlx_lm.server \
            --model "$LLM_MODEL" \
            --port "$LLM_PORT" \
            --host 0.0.0.0
    else
        if [ ! -f "$LLM_MODEL" ]; then
            echo "==> strategic_llm: GGUF model not found at $LLM_MODEL — run 'make models'" >&2
            return 1
        fi
        component_start strategic_llm \
            "$SVENV/bin/python" -m llama_cpp.server \
            --model "$LLM_MODEL" \
            --port "$LLM_PORT" \
            --host 0.0.0.0 \
            --n_ctx "$LLM_CTX"
    fi
}

case "${1:-status}" in
    start)   _start ;;
    stop)    component_stop strategic_llm ;;
    restart) component_stop strategic_llm; sleep 1; _start ;;
    status)  component_status strategic_llm ;;
    health)
        _resolve
        if component_check_healthy "http://localhost:${LLM_PORT}/v1/models"; then
            printf "%-22s %s\n" "strategic_llm" "healthy"
        else
            printf "%-22s %s\n" "strategic_llm" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
