#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT

"$LOOP_DIR/stop_loop.sh"
sleep 2
"$LOOP_DIR/start_loop.sh"
