"""VLM holistic image evaluation scorer — FastAPI HTTP sidecar.

Exposes POST /score → { image_uuid, photorealism, anatomical_coherence,
interaction_plausibility, lighting_consistency, prompt_adherence, issues, recommendations }.

Model is loaded once at startup.  temperature=0 for deterministic output.
A threading.Lock serialises inference since llama_cpp is not thread-safe.
"""

import base64
import json
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from llama_cpp import Llama
from llama_cpp.llama_chat_format import Qwen25VLChatHandler
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()
_vlm_cfg = _cfg.models["vlm"]
_vlm_compute = _cfg.compute["vlm_scorer"]

model_dir = Path(_vlm_cfg["dir"])
_chat_handler = Qwen25VLChatHandler(
    clip_model_path=str(model_dir / _vlm_cfg["mmproj_filename"])
)
llm = Llama(
    model_path=str(model_dir / _vlm_cfg["filename"]),
    chat_handler=_chat_handler,
    n_ctx=_vlm_cfg["context_length"],
    n_gpu_layers=_vlm_compute["n_gpu_layers"],
    logits_all=True,
)

_infer_lock = threading.Lock()

EVAL_PROMPT = """Evaluate this image on the following criteria and return a JSON object:
{{
  "photorealism": <0-10>,
  "anatomical_coherence": <0-10>,
  "interaction_plausibility": <0-10>,
  "lighting_consistency": <0-10>,
  "prompt_adherence": <0-10>,
  "issues": [<list of specific issues observed>],
  "recommendations": [<list of specific prompt or parameter adjustments>]
}}
Original prompt: {prompt}
Return only valid JSON, no preamble."""

_SCORE_FIELDS = (
    "photorealism",
    "anatomical_coherence",
    "interaction_plausibility",
    "lighting_consistency",
    "prompt_adherence",
)

app = FastAPI()


class ScoreRequest(BaseModel):
    image_uuid: str
    image_path: str
    prompt: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/score")
def score(req: ScoreRequest) -> dict[str, Any]:
    try:
        with open(req.image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    ext = Path(req.image_path).suffix.lower().lstrip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext or "png"
    image_url = f"data:image/{mime};base64,{image_b64}"

    with _infer_lock:
        response = llm.create_chat_completion(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": EVAL_PROMPT.format(prompt=req.prompt)},
                    ],
                }
            ],
            max_tokens=512,
            temperature=0.0,
            stream=False,
        )

    raw = response["choices"][0]["message"]["content"]
    try:
        result: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"VLM returned malformed JSON: {exc}  raw={raw[:200]!r}",
        )

    for field in _SCORE_FIELDS:
        if field in result and isinstance(result[field], (int, float)):
            result[field] = max(0.0, min(10.0, float(result[field])))
    result["image_uuid"] = req.image_uuid
    return result
