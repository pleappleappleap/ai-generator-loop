#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../config.sh
source "$SCRIPT_DIR/../../config.sh"
cd "$AI_IMAGE_SCORERS"
source venv/bin/activate
