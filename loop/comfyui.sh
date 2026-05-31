#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SOXHLET_ROOT="${SOXHLET_ROOT:-$(dirname "$LOOP_DIR")}"
export SOXHLET_ROOT
. "$LOOP_DIR/lib/component.sh"

_health_url() {
    local url
    url=$(yq '.broker.comfyui_url // "http://127.0.0.1:8188"' "$CFG" 2>/dev/null)
    echo "${url:-http://127.0.0.1:8188}/system_stats"
}

case "${1:-status}" in
    start)   component_start comfyui "$COMFYUI_DIR/launch.sh" ;;
    stop)    component_stop comfyui ;;
    restart) component_restart comfyui "$COMFYUI_DIR/launch.sh" ;;
    status)  component_status comfyui ;;
    health)
        if component_check_healthy "$(_health_url)"; then
            printf "%-22s %s\n" "comfyui" "healthy"
        else
            printf "%-22s %s\n" "comfyui" "not ready"
            exit 1
        fi ;;
    *)       echo "Usage: $0 {start|stop|restart|status|health}" >&2; exit 1 ;;
esac
