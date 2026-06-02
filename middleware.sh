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

_start_port_forwards() {
    mkdir -p "$PIDDIR"
    kubectl port-forward svc/postgres 12007:5432 -n "$NS" \
        >"$PIDDIR/pf-postgres.log" 2>&1 &
    echo $! > "$PIDDIR/pf-postgres.pid"
    kubectl port-forward svc/artemis 12008:61616 12009:8161 -n "$NS" \
        >"$PIDDIR/pf-artemis.log" 2>&1 &
    echo $! > "$PIDDIR/pf-artemis.pid"
    kubectl port-forward svc/searxng 12010:8080 -n "$NS" \
        >"$PIDDIR/pf-searxng.log" 2>&1 &
    echo $! > "$PIDDIR/pf-searxng.pid"
    printf "==> Port-forwards started (postgres→12007, artemis→12008/12009, searxng→12010)\n"
}

_stop_port_forwards() {
    for f in "$PIDDIR/pf-postgres.pid" "$PIDDIR/pf-artemis.pid" "$PIDDIR/pf-searxng.pid"; do
        [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && rm -f "$f"
    done
}

_start() {
    _require_kubectl
    kubectl apply -k "$SOXHLET_ROOT/middleware/k8s/"
    _wait_pod_ready "app=postgres" "PostgreSQL" 120
    _wait_pod_ready "app=artemis"  "Artemis"    120
    _wait_pod_ready "app=searxng"  "SearXNG"    120
    _start_port_forwards
    _status
}

_stop() {
    _stop_port_forwards
    _require_kubectl
    kubectl scale deployment artemis --replicas=0 -n "$NS" 2>/dev/null || true
    kubectl scale deployment searxng --replicas=0 -n "$NS" 2>/dev/null || true
    echo "==> Artemis and SearXNG scaled to 0"
    echo "==> PostgreSQL left running — use 'middleware.sh stop --all' to fully tear down"
}

_stop_all() {
    _stop_port_forwards
    _require_kubectl
    kubectl delete -k "$SOXHLET_ROOT/middleware/k8s/" 2>/dev/null || true
    echo "==> All middleware resources deleted"
}

_restart() {
    _require_kubectl
    _stop_port_forwards
    kubectl apply -k "$SOXHLET_ROOT/middleware/k8s/"
    kubectl rollout restart deployment/artemis deployment/searxng -n "$NS" 2>/dev/null || true
    _wait_pod_ready "app=postgres" "PostgreSQL" 120
    _wait_pod_ready "app=artemis"  "Artemis"    120
    _wait_pod_ready "app=searxng"  "SearXNG"    120
    _start_port_forwards
    _status
}

_status() {
    _require_kubectl
    echo "==> Middleware pods (namespace: $NS):"
    kubectl get pods -n "$NS" -l 'app in (artemis,postgres,searxng)' 2>&1
    echo ""
    echo "==> Endpoints:"
    echo "  PostgreSQL:       localhost:12007  (pipeline / pipeline)"
    echo "  Artemis OpenWire: localhost:12008"
    echo "  Artemis console:  http://localhost:12009  (admin / admin)"
    echo "  SearXNG:          http://localhost:12010"
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
    if _pod_ready "app=searxng"; then
        printf "%-22s %s\n" "searxng" "healthy"
    else
        printf "%-22s %s\n" "searxng" "not ready"
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
