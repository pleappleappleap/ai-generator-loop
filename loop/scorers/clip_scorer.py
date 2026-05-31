"""CLIP semantic similarity scorer — FastAPI HTTP sidecar.

Exposes POST /score → { image_uuid, clip_score, image_embedding, prompt_embedding }.
Model is loaded once at startup and held resident.  Stateless: no broker connection.
"""

import math
import sys
from pathlib import Path
from typing import Any

import open_clip
import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402
from config import resolve_backend

_cfg = _load_config()

model, _, preprocess = open_clip.create_model_and_transforms(
    _cfg.models["clip"]["name"],
    pretrained=_cfg.models["clip"]["pretrained"],
)
tokenizer = open_clip.get_tokenizer(_cfg.models["clip"]["name"])

_backend = resolve_backend(_cfg.compute["clip_scorer"]["backend"])
if _backend == "mps":
    _device = torch.device("mps")
elif _backend in ("cuda", "rocm"):
    _device = torch.device("cuda")
else:
    _device = torch.device("cpu")

model = model.to(_device)

app = FastAPI()


class ScoreRequest(BaseModel):
    image_uuid: str
    image_path: str
    prompt: str


class EmbedTextRequest(BaseModel):
    text: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/embed_text")
def embed_text(req: EmbedTextRequest) -> dict[str, Any]:
    text = tokenizer([req.text]).to(_device)
    with torch.no_grad():
        text_features = model.encode_text(text)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        embedding = text_features.squeeze().tolist()
    return {"embedding": embedding}


@app.post("/score")
def score(req: ScoreRequest) -> dict[str, Any]:
    try:
        image = preprocess(Image.open(req.image_path)).unsqueeze(0).to(_device)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    text = tokenizer([req.prompt]).to(_device)
    with torch.no_grad():
        image_features = model.encode_image(image)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = model.encode_text(text)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        similarity = (image_features @ text_features.T).item()
        image_embedding = image_features.squeeze().tolist()
        prompt_embedding = text_features.squeeze().tolist()
    if math.isnan(similarity) or math.isinf(similarity):
        similarity = 0.0
    return {
        "image_uuid": req.image_uuid,
        "clip_score": max(-1.0, min(1.0, similarity)),
        "image_embedding": image_embedding,
        "prompt_embedding": prompt_embedding,
    }
