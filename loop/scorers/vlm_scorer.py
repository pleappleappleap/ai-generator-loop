"""VLM holistic image evaluation scorer — FastAPI HTTP sidecar.

macOS:          mlx-vlm (MLX native, Metal acceleration).
Linux/Windows:  llama.cpp (GGUF, CUDA or CPU).

Both backends expose identical HTTP endpoints:
  GET  /health  → { status: "ok" }
  POST /score   → { image_uuid, photorealism, anatomical_coherence,
                    interaction_plausibility, lighting_consistency,
                    prompt_adherence, issues, recommendations }
  POST /analyze → { answer }
"""

import base64
import io
import json
import subprocess
import sys
import threading
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()
_vlm_cfg = _cfg.models["vlm"]
_vlm_compute = _cfg.compute["vlm_scorer"]

_IS_MACOS = sys.platform == "darwin"
_infer_lock = threading.Lock()

# ── Backend initialisation ────────────────────────────────────────────────────

# Forward-declare platform-specific names so pyright sees them as always bound.
_mlx_generate: Any = None
_mlx_model: Any = None
_mlx_processor: Any = None
_mlx_config: Any = None
_llm: Any = None


def _ensure_quantized(base_path: str) -> str:
    """Quantize the VLM model to Q8 on first run and save alongside the original.

    Subsequent startups detect the saved q8 directory and skip quantization.
    Returns base_path unchanged if the model directory does not exist (e.g. in tests).
    """
    base = Path(base_path)
    if not base_path or not base.exists():
        return base_path
    q_path = Path(base_path + "-q8")
    if q_path.exists() and any(q_path.glob("*.safetensors")):
        return str(q_path)
    print(f"[vlm_scorer] Quantizing VLM to Q8 (one-time) → {q_path}", flush=True)
    subprocess.run(
        [sys.executable, "-m", "mlx_vlm.convert",
         "--hf-path", base_path,
         "--mlx-path", str(q_path),
         "--quantize", "--q-bits", "8"],
        check=True,
    )
    return str(q_path)


if _IS_MACOS:
    import mlx.core as mx  # noqa: F401  — ensure MLX is importable
    from mlx_vlm import generate as _mlx_generate
    from mlx_vlm import load as _mlx_load
    from mlx_vlm.utils import load_config as _mlx_load_config

    _model_path = _ensure_quantized(
        str(Path(_vlm_cfg["dir"]) / _vlm_cfg.get("mlx_model_name", ""))
    )
    _mlx_model, _mlx_processor = _mlx_load(_model_path)
    _mlx_config = _mlx_load_config(_model_path)

else:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import Qwen25VLChatHandler

    _model_dir = Path(_vlm_cfg["dir"])
    _chat_handler = Qwen25VLChatHandler(
        clip_model_path=str(_model_dir / _vlm_cfg["mmproj_filename"])
    )
    _llm = Llama(
        model_path=str(_model_dir / _vlm_cfg["filename"]),
        chat_handler=_chat_handler,
        n_ctx=_vlm_cfg.get("context_length", 4096),
        n_gpu_layers=_vlm_compute.get("n_gpu_layers", -1),
        logits_all=True,
    )


# ── Image source normalisation ────────────────────────────────────────────────

def _to_data_uri(image_source: str) -> str:
    """Normalise any image source to a data: URI for llama.cpp."""
    if image_source.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(image_source) as resp:
                image_bytes = resp.read()
                content_type = (
                    resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
                )
        except Exception as exc:
            raise OSError(str(exc)) from exc
        return f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"
    if image_source.startswith("data:"):
        return image_source
    if image_source.startswith("file://"):
        image_source = urllib.parse.urlparse(image_source).path
    with open(image_source, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()
    ext = Path(image_source).suffix.lower().lstrip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext or "png"
    return f"data:image/{mime};base64,{image_b64}"


def _to_mlx_image(image_source: str):
    """Resolve an image source for mlx-vlm.

    Qwen3-VL's processor needs to read image dimensions from a local file to
    compute the correct number of <image_pad> tokens. HTTP URLs are downloaded
    to a temp file; data URIs are decoded to a PIL Image object.
    """
    if image_source.startswith("data:"):
        from PIL import Image as PILImage
        _, data = image_source.split(",", 1)
        return PILImage.open(io.BytesIO(base64.b64decode(data)))
    if image_source.startswith("file://"):
        return urllib.parse.urlparse(image_source).path
    if image_source.startswith(("http://", "https://")):
        with urllib.request.urlopen(image_source) as resp:
            data = resp.read()
        suffix = Path(image_source.split("?")[0]).suffix or ".png"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(data)
        tmp.close()
        return tmp.name
    return image_source  # local path


# ── Unified inference entry point ─────────────────────────────────────────────

def _call_vlm(image_source: str, prompt: str, max_tokens: int) -> str:
    """Run VLM inference on an image + prompt and return the raw text response.

    Args:
        image_source: Local file path, http(s) URL, or data: URI.
        prompt:       Text prompt / question for the VLM.
        max_tokens:   Maximum tokens in the response.

    Raises:
        OSError: On I/O failure (unreadable file, unreachable URL).
    """
    prompt = "/no_think\n" + prompt
    if _IS_MACOS:
        try:
            img = _to_mlx_image(image_source)
            with _infer_lock:
                return _mlx_generate(
                    _mlx_model, _mlx_processor,
                    image=img, prompt=prompt,
                    max_tokens=max_tokens, temp=0.0, verbose=False,
                )
        except OSError:
            raise
        except Exception as exc:
            raise OSError(str(exc)) from exc
    else:
        url = _to_data_uri(image_source)  # raises OSError on I/O failure
        with _infer_lock:
            response = _llm.create_chat_completion(
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": prompt},
                ]}],
                max_tokens=max_tokens,
                temperature=0.0,
                stream=False,
            )
        return response["choices"][0]["message"]["content"]


# ── FastAPI application ───────────────────────────────────────────────────────

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
    eval_prompt: str | None = None


class AnalyzeRequest(BaseModel):
    image_url: str
    question: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/score")
def score(req: ScoreRequest) -> dict[str, Any]:
    try:
        prompt_text = req.eval_prompt if req.eval_prompt else EVAL_PROMPT.format(prompt=req.prompt)
        raw = _call_vlm(req.image_path, prompt_text, 512)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
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


@app.post("/analyze")
def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    try:
        answer = _call_vlm(req.image_url, req.question, 512)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"answer": answer}
