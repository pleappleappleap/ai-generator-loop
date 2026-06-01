# Scorers

Three independent Python HTTP sidecars that evaluate each generated image in parallel.
The Spring Boot pipeline calls them directly via HTTP — no broker connection.

## Components

| Component | Language | Role |
|-----------|----------|------|
| `clip_scorer.py` | Python (FastAPI) | ViT-L-14 CLIP semantic similarity; `/score` + `/embed_text` |
| `artifact_scorer.py` | Python (FastAPI) | AI-image-detector artifact confidence; `/score` |
| `vlm_scorer.py` | Python (FastAPI) | Qwen3-VL-8B holistic evaluation; `/score` + `/analyze` |

## Activating the Python Environment

```bash
source ~/soxhlet/loop/scorers/activate.sh
```

## Running Tests

```bash
cd ~/soxhlet/loop/scorers
venv/bin/pytest tests/ -v
```

The `tests/` directory covers:
- `test_clip_scorer.py`  -  CLIP scorer unit tests
- `test_artifact_scorer.py`  -  artifact scorer unit tests
- `test_vlm_scorer.py`  -  VLM scorer unit tests

### Full suite (lint + typecheck + Python tests)

```bash
cd ~/soxhlet/loop
make all
```

## Adding a New Scorer

1. Create `<name>_scorer.py` following the pattern in `clip_scorer.py`
2. Expose a `POST /score` endpoint that accepts `{ image_path: string }` and returns
   scorer-specific JSON
3. Add the scorer URL to `config.yaml` under `sidecars.<name>_scorer`
4. Call the new scorer from `Scorer.java` alongside the existing three
5. Add the score fields to the `images` PostgreSQL table (Flyway migration)
6. Add tests to `tests/test_<name>_scorer.py`

## Model Storage

```
models/
+-- artifact-detector/    umm-maybe/AI-image-detector (HuggingFace snapshot)
+-- vlm/                  macOS: Qwen3-VL-8B-Instruct-abliterated-v2/ (safetensors snapshot;
|                                quantized to Q8 on first startup → vlm/…-q8/)
|                         Linux: Qwen3-VL-8B-Instruct-abliterated-v2.Q8_0.gguf
|                                + Qwen3-VL-8B-Instruct-abliterated-v2.mmproj-Q8_0.gguf
+-- tactical/             macOS: Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-4bit/
|                                (MLX 4-bit directory, served by mlx_lm.server)
|                         Linux: Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated.Q4_K_M.gguf
|                                (GGUF, served by llama_cpp.server)
\-- strategic/            macOS: Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated/
                                 (bf16 safetensors, served by mlx_lm.server)
                          Linux: Huihui-Qwen3-Next-80B-A3B-Thinking-abliterated.Q4_K_M.gguf
                                 (GGUF, served by llama_cpp.server)
```
