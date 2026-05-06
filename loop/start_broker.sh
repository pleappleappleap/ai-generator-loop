#!/bin/sh
# Starts ActiveMQ Artemis.
#
# First-time setup (run once):
#   artemis create "$AI_IMAGE_ROOT/loop/artemis-broker"
#   Then enable both acceptors in artemis-broker/etc/broker.xml:
#     <acceptor name="amqp">tcp://0.0.0.0:5672?protocols=AMQP</acceptor>
#     <acceptor name="stomp">tcp://0.0.0.0:61613?protocols=STOMP</acceptor>
#   See INSTALL.md section 3 for details.
AI_IMAGE_ROOT="${1:-${AI_IMAGE_ROOT:-}}"
if [ -z "$AI_IMAGE_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    AI_IMAGE_ROOT="$PWD"
fi
: "${AI_IMAGE_ROOT:?provide root as \$1, set AI_IMAGE_ROOT in environment, or run from the repo root}"
CFG="$AI_IMAGE_ROOT/config.yaml"

ARTEMIS_DATA=$(yq '.broker.artemis_data' "$CFG")
[ "$ARTEMIS_DATA" = "auto" ] && ARTEMIS_DATA="$AI_IMAGE_ROOT/loop/artemis-broker"

if [ ! -x "$ARTEMIS_DATA/bin/artemis" ]; then
    echo "ERROR: Artemis broker not found at $ARTEMIS_DATA" >&2
    echo "       Create it first:" >&2
    echo "         artemis create \"$ARTEMIS_DATA\"" >&2
    echo "       Then enable AMQP and STOMP acceptors in $ARTEMIS_DATA/etc/broker.xml" >&2
    echo "       (See INSTALL.md section 3 for details.)" >&2
    exit 1
fi

"$ARTEMIS_DATA/bin/artemis" run &

_HOST=$(hostname 2>/dev/null || echo "localhost")
echo "==> Artemis console: http://${_HOST}:8161  (admin / admin)"
