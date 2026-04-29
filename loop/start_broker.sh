#!/bin/sh
# Starts ActiveMQ Artemis and Redis.
#
# First-time setup (run once):
#   artemis create "$AI_IMAGE_ROOT/loop/artemis-broker"
#   Then add the AMQP 0-9-1 acceptor to artemis-broker/etc/broker.xml:
#     <acceptor name="amqp091">tcp://0.0.0.0:5672?protocols=AMQP;amqpCredits=1000</acceptor>
#   See loop/artemis-amqp091-acceptor.xml for the snippet to add.
#
# Install: brew install activemq
AI_IMAGE_ROOT="${1:-${AI_IMAGE_ROOT:-}}"
if [ -z "$AI_IMAGE_ROOT" ] && [ -f "$PWD/config.yaml" ] && [ -d "$PWD/loop" ]; then
    AI_IMAGE_ROOT="$PWD"
fi
: "${AI_IMAGE_ROOT:?provide root as \$1, set AI_IMAGE_ROOT in environment, or run from the repo root}"
CFG="$AI_IMAGE_ROOT/config.yaml"

ARTEMIS_DATA=$(yq '.broker.artemis_data' "$CFG")
[ "$ARTEMIS_DATA" = "auto" ] && ARTEMIS_DATA="$AI_IMAGE_ROOT/loop/artemis-broker"

"$ARTEMIS_DATA/bin/artemis" run &
redis-server --daemonize yes
