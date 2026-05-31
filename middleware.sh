#!/bin/sh
SOXHLET_ROOT="$(cd "$(dirname "$0")" && pwd)"
export SOXHLET_ROOT
. "$SOXHLET_ROOT/loop/lib/component.sh"
NS=pipeline

_require_kubectl() {
    if ! command -v kubectl >/dev/null 2>&1; then
        echo "ERROR: kubectl not found. Install via 'brew install kubectl' or ensure K3s is set up." >&2
        exit 1
    fi
}

_pod_ready() {
    local label="$1"
    kubectl get pods -n "$NS" -l "$label" \
        -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' \
        2>/dev/null | grep -q "True"
}

_wait_pod_ready() {
    local label="$1" name="$2" timeout="${3:-120}"
    printf "==> Waiting for %s" "$name"
    local elapsed=0 interval=2
    while ! _pod_ready "$label"; do
        if [ "$elapsed" -ge "$timeout" ]; then
            echo ""
            echo "ERROR: $name pod did not become Ready within ${timeout}s" >&2
            kubectl get pods -n "$NS" -l "$label" >&2
            exit 1
        fi
        sleep "$interval"; elapsed=$((elapsed + interval)); printf "."
    done
    echo " ready (${elapsed}s)"
}

_start() {
    _require_kubectl
    kubectl apply -k "$SOXHLET_ROOT/middleware/k8s/"
    _wait_pod_ready "app=postgres" "PostgreSQL" 120
    _wait_pod_ready "app=artemis"  "Artemis"    120
    _status
}

_stop() {
    _require_kubectl
    kubectl scale deployment artemis --replicas=0 -n "$NS" 2>/dev/null || true
    echo "==> Artemis scaled to 0"
    echo "==> PostgreSQL left running — use 'middleware.sh stop --all' to fully tear down"
}

_stop_all() {
    _require_kubectl
    kubectl delete -k "$SOXHLET_ROOT/middleware/k8s/" 2>/dev/null || true
    echo "==> All middleware resources deleted"
}

_restart() {
    _require_kubectl
    kubectl rollout restart deployment/artemis -n "$NS"
    _wait_pod_ready "app=artemis" "Artemis" 120
}

_status() {
    _require_kubectl
    echo "==> Middleware pods (namespace: $NS):"
    kubectl get pods -n "$NS" -l 'app in (artemis,postgres)' 2>&1
    echo ""
    _HOST=$(kubectl get nodes \
        -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' \
        2>/dev/null || echo "localhost")
    echo "==> Endpoints:"
    echo "  Artemis OpenWire: tcp://${_HOST}:30616"
    echo "  Artemis AMQP:     ${_HOST}:30672"
    echo "  Artemis console:  http://${_HOST}:30161  (admin / admin)"
    echo "  PostgreSQL:       ${_HOST}:5432  (pipeline / pipeline)"
}

_health() {
    _require_kubectl
    local ok=0
    if _pod_ready "app=postgres"; then
        printf "%-22s %s\n" "postgresql" "healthy"
    else
        printf "%-22s %s\n" "postgresql" "not ready"
        ok=1
    fi
    if _pod_ready "app=artemis"; then
        printf "%-22s %s\n" "artemis" "healthy"
    else
        printf "%-22s %s\n" "artemis" "not ready"
        ok=1
    fi
    return $ok
}

case "${1:-status}" in
    start)   _start ;;
    stop)
        case "${2:-}" in
            --all) _stop_all ;;
            *)     _stop ;;
        esac ;;
    restart) _restart ;;
    status)  _status ;;
    health)  _health ;;
    *)       echo "Usage: $0 {start|stop [--all]|restart|status|health}" >&2; exit 1 ;;
esac
