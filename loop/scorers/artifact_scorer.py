"""AI artifact detection scorer — FastAPI HTTP sidecar.

Exposes POST /score → { image_uuid, ai_confidence }.
Model is loaded once at startup and held resident.  Stateless: no broker connection.
"""

import sys
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from transformers import pipeline

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402
from config import resolve_backend

_cfg = _load_config()

_backend = resolve_backend(_cfg.compute["artifact_scorer"]["backend"])
_device = 0 if _backend in ("cuda", "rocm") else (_backend if _backend == "mps" else -1)

detector = pipeline(
    "image-classification",
    model=_cfg.models["artifact_detector"]["dir"],
    device=_device,
)

app = FastAPI()


class ScoreRequest(BaseModel):
    image_uuid: str
    image_path: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/score")
def score(req: ScoreRequest) -> dict[str, Any]:
    image_path = urllib.parse.urlparse(req.image_path).path if req.image_path.startswith("file://") else req.image_path
    results = detector(image_path)
    ai_confidence = next((r["score"] for r in results if r["label"] == "artificial"), 0.0)
    return {
        "image_uuid": req.image_uuid,
        "ai_confidence": max(0.0, min(1.0, ai_confidence)),
    }
