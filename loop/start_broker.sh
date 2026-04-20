#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../config.sh
source "$SCRIPT_DIR/../config.sh"

export PATH="$PATH:$AI_IMAGE_HOMEBREW_PREFIX/sbin"
rabbitmq-server -detached
redis-server --daemonize yes
