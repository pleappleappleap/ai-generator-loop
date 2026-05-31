# ComfyUI

Stable Diffusion XL generation engine. Runs as a local HTTP server
on the port configured in `config.yaml` (default 12006). The pipeline
communicates with it exclusively via its REST API and WebSocket interface.

## Starting ComfyUI

ComfyUI is started automatically by `loop.sh start`. To manage it
independently:

```bash
~/soxhlet/loop/comfyui.sh start
~/soxhlet/loop/comfyui.sh stop
~/soxhlet/loop/comfyui.sh health
```

## Model Directory Layout

```
models/
+-- checkpoints/        SDXL base and inpainting checkpoints
+-- controlnet/         SDXL ControlNet models
+-- sam/                Segment Anything Model (sam_vit_h_4b8939.pth)
\-- grounding-dino/     Grounding DINO (groundingdino_swint_ogc.pth)
```

## Workflow Development

1. Start ComfyUI via `loop.sh start` and open `http://127.0.0.1:<port>`
2. Enable Dev Mode in Settings
3. Build workflow in browser UI
4. Click Save (API Format) to export to `loop/workflows/`

```
loop/workflows/
\-- sdxl_base.json
```

## Custom Nodes

Installed automatically by `make all`:

- comfyui-inpaint-nodes (github.com/Acly/comfyui-inpaint-nodes)
- comfyui_segment_anything (github.com/storyicon/comfyui_segment_anything)
