#!/bin/bash
# Starts ActiveMQ Artemis and Redis.
#
# First-time setup (run once):
#   artemis create "$AI_IMAGE_ROOT/loop/artemis-broker"
#   Then add the AMQP 0-9-1 acceptor to artemis-broker/etc/broker.xml:
#     <acceptor name="amqp091">tcp://0.0.0.0:5672?protocols=AMQP;amqpCredits=1000</acceptor>
#   See loop/artemis-amqp091-acceptor.xml for the snippet to add.
#
# Install: brew install activemq
AI_IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$AI_IMAGE_ROOT/config.yaml"

ARTEMIS_DATA=$(yq '.broker.artemis_data' "$CFG")

"$ARTEMIS_DATA/bin/artemis" run &
redis-server --daemonize yes
