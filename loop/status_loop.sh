#!/bin/sh
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_IMAGE_ROOT="${AI_IMAGE_ROOT:-$(dirname "$LOOP_DIR")}"
export AI_IMAGE_ROOT
. "$LOOP_DIR/lib/component.sh"

printf "%-22s %s\n" "PROCESS" "STATUS"
printf "%-22s %s\n" "-------" "------"

for comp in comfyui llama_cpp clip_scorer artifact_scorer vlm_scorer pipeline; do
    component_status "$comp"
done

echo ""
"$LOOP_DIR/broker.sh" status
