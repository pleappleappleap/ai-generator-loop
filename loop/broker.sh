#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
NS=pipeline

_require_kubectl() {
    if ! command -v kubectl >/dev/null 2>&1; then
        echo "ERROR: kubectl not found. Install via 'brew install kubectl' or start Colima with --kubernetes." >&2
        exit 1
    fi
}

_status() {
    _require_kubectl
    echo "==> K3s pods (namespace: $NS):"
    kubectl get pods -n "$NS" -l 'app in (artemis,postgres)' 2>&1
    echo ""
    echo "==> Artemis endpoints:"
    _HOST=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "localhost")
    echo "  console: http://${_HOST}:30161  (admin / admin)"
    echo "  AMQP:    ${_HOST}:30672"
    echo "  STOMP:   ${_HOST}:30613"
}

_start() {
    _require_kubectl
    kubectl apply -k "$AI_IMAGE_ROOT/k8s/"
    echo "==> Waiting for Artemis to be ready..."
    kubectl rollout status deployment/artemis -n "$NS" --timeout=120s
    echo "==> Waiting for PostgreSQL to be ready..."
    kubectl rollout status statefulset/postgres -n "$NS" --timeout=120s
    _status
}

_stop() {
    _require_kubectl
    kubectl scale deployment artemis --replicas=0 -n "$NS" 2>/dev/null || true
    echo "==> Artemis scaled to 0 (PostgreSQL left running — use 'kubectl delete -k k8s/' to fully tear down)"
}

_restart() {
    _require_kubectl
    kubectl rollout restart deployment/artemis -n "$NS"
    kubectl rollout status deployment/artemis -n "$NS" --timeout=120s
}

case "${1:-status}" in
    start)   _start ;;
    stop)    _stop ;;
    restart) _restart ;;
    status)  _status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
