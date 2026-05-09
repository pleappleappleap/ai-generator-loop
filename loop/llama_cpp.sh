#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

_resolve() {
    LLM_SERVER_URL=$(yq '.tactical.model.server_url // "http://localhost:8080/v1"' "$CFG" 2>/dev/null)
    : "${LLM_SERVER_URL:=http://localhost:8080/v1}"
    LLM_PORT=$(echo "$LLM_SERVER_URL" | sed 's|.*:\([0-9]*\)/.*|\1|; s|http://[^:]*||')
    : "${LLM_PORT:=8080}"

    LLM_DIR=$(yq '.tactical.model.dir' "$CFG" 2>/dev/null)
    [ "$LLM_DIR" = "auto" ] || [ -z "$LLM_DIR" ] && LLM_DIR="$SCORERS/models/tactical"
    LLM_FILENAME=$(yq '.tactical.model.filename' "$CFG" 2>/dev/null)
    LLM_MODEL="$LLM_DIR/$LLM_FILENAME"
    LLM_GPU_LAYERS=$(yq '.compute.tactical_llm.n_gpu_layers // -1' "$CFG" 2>/dev/null)
    : "${LLM_GPU_LAYERS:=-1}"
}

_start() {
    _resolve
    if [ ! -f "$LLM_MODEL" ]; then
        echo "==> llama_cpp: model not found at $LLM_MODEL — run 'make models'" >&2
        return 1
    fi
    component_start llama_cpp \
        "$SVENV/bin/python" -m llama_cpp.server \
        --model "$LLM_MODEL" \
        --n_gpu_layers "$LLM_GPU_LAYERS" \
        --host 0.0.0.0 \
        --port "$LLM_PORT"
}

case "${1:-status}" in
    start)   _start ;;
    stop)    component_stop llama_cpp ;;
    restart) component_stop llama_cpp; sleep 1; _start ;;
    status)  component_status llama_cpp ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
