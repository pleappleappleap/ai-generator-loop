# ComfyUI

Stable Diffusion XL generation engine. Runs as a local HTTP server
at http://127.0.0.1:8188. The pipeline communicates with it exclusively
via its REST API and WebSocket interface.

## Starting ComfyUI

```bash
~/ai-image/loop/ComfyUI/launch.sh
```

## Model Directory Layout

```
models/
├── checkpoints/        SDXL base and inpainting checkpoints
├── controlnet/         SDXL ControlNet models
├── sam/                Segment Anything Model (sam_vit_h_4b8939.pth)
└── grounding-dino/     Grounding DINO (groundingdino_swint_ogc.pth)
```

## Workflow Development

1. Start ComfyUI and open http://127.0.0.1:8188
2. Enable Dev Mode in Settings
3. Build workflow in browser UI
4. Click Save (API Format) to export

```
workflows/
├── two_character_base.json
└── inpainting_base.json
```

## Custom Nodes (install via ComfyUI Manager)

- ComfyUI Impact Pack
- ComfyUI ControlNet Auxiliary Preprocessors
- ComfyUI Advanced ControlNet
- ComfyUI Segment Anything
- ComfyUI Grounding DINO
