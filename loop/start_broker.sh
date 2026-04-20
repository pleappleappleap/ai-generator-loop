#!/bin/bash
AI_IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$AI_IMAGE_ROOT/config.yaml"

HOMEBREW_PREFIX=$(yq '.system.homebrew_prefix' "$CFG")
export PATH="$PATH:$HOMEBREW_PREFIX/sbin"
rabbitmq-server -detached
redis-server --daemonize yes
