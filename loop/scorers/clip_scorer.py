"""CLIP semantic similarity scorer.

Computes cosine similarity between a generated image and its prompt
using the OpenAI ViT-L-14 CLIP model. Also produces the 512-dimensional
normalized image embedding for storage in LanceDB.

The model is loaded once at process startup and held resident. Scoring
requests are processed on worker threads, leaving the main thread free
to handle cancel events from the aggregator.

Each scorer process maintains an active_jobs dict mapping image UUIDs
to threading.Event cancel flags. When a cancel event arrives for an
in-flight image, the flag is set and the worker thread exits at the
next cancellation checkpoint without publishing a result.

Address subscriptions (STOMP / Artemis):
  /topic/scorer.requests  (durable: scorer.clip.requests)  → score requests
  /topic/scorer.events    (durable: scorer.clip.events)    → cancel events

Address publications (STOMP / Artemis):
  /queue/aggregator.clip.queue  → clip score results
"""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import open_clip
import stomp
import torch
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config, resolve_backend  # noqa: E402

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

active_jobs: dict[str, threading.Event] = {}
"""Map of image_uuid to cancel Event for in-flight scoring jobs."""

jobs_lock = threading.Lock()
"""Lock protecting active_jobs for concurrent access."""

_u = urlparse(_cfg.broker["stomp_url"])
_STOMP_HOST: str = _u.hostname or "localhost"
_STOMP_PORT: int = _u.port or 61613
_STOMP_USER: str = _u.username or ""
_STOMP_PASS: str = _u.password or ""

_SUB_REQUESTS = "1"
_SUB_EVENTS   = "2"


def score(image_path: str, prompt: str) -> dict[str, Any]:
    """Compute CLIP similarity score and image embedding synchronously.

    Args:
        image_path: Filesystem path or URL to the image file.
        prompt: Text prompt to compare against the image.

    Returns:
        Dict with keys:
            clip_score (float): Cosine similarity between image and
                text embeddings. Range approximately 0.0–1.0.
            image_embedding (list[float]): 512-dimensional normalized
                CLIP image embedding vector.
    """
    image = preprocess(Image.open(image_path)).unsqueeze(0).to(_device)
    text = tokenizer([prompt]).to(_device)
    with torch.no_grad():
        image_features = model.encode_image(image)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = model.encode_text(text)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        similarity = (image_features @ text_features.T).item()
        image_embedding = image_features.squeeze().tolist()
    return {"clip_score": similarity, "image_embedding": image_embedding}


def score_worker(
    image_uuid: str,
    image_path: str,
    prompt: str,
    cancel_event: threading.Event,
    conn: stomp.Connection,
) -> None:
    """Score an image on a worker thread with cancellation support.

    Checks the cancel_event between each major inference operation.
    If cancelled, exits without publishing a result. Removes the job
    from active_jobs on exit regardless of outcome.

    Publishes to /queue/aggregator.clip.queue on successful completion.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        prompt: Text prompt to compare against the image.
        cancel_event: threading.Event set by the cancel handler if the
            aggregator requests cancellation for this image_uuid.
        conn: STOMP connection for publishing the result (thread-safe).
    """
    try:
        image = preprocess(Image.open(image_path)).unsqueeze(0).to(_device)
        text = tokenizer([prompt]).to(_device)
        if cancel_event.is_set():
            return
        with torch.no_grad():
            image_features = model.encode_image(image)
            if cancel_event.is_set():
                return
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = model.encode_text(text)
            if cancel_event.is_set():
                return
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            similarity = (image_features @ text_features.T).item()
            image_embedding = image_features.squeeze().tolist()
        result = {
            "image_uuid":      image_uuid,
            "clip_score":      similarity,
            "image_embedding": image_embedding,
        }
        conn.send(
            destination="/queue/aggregator.clip.queue",
            body=json.dumps(result),
            headers={"persistent": "true"},
        )
    finally:
        with jobs_lock:
            active_jobs.pop(image_uuid, None)


class _Listener(stomp.ConnectionListener):
    """STOMP message listener for the CLIP scorer."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[clip_scorer] STOMP error: {frame.body}", file=sys.stderr)

    def on_message(self, frame: stomp.utils.Frame) -> None:
        sub_id = frame.headers.get("subscription", "")
        msg_id = frame.headers.get("message-id", "")
        try:
            if sub_id == _SUB_REQUESTS:
                request = json.loads(frame.body)
                image_uuid = request["image_uuid"]
                cancel_event = threading.Event()
                with jobs_lock:
                    active_jobs[image_uuid] = cancel_event
                t = threading.Thread(
                    target=score_worker,
                    args=(image_uuid, request["image_path"], request["prompt"],
                          cancel_event, self._conn),
                    daemon=True,
                )
                t.start()
            elif sub_id == _SUB_EVENTS:
                msg = json.loads(frame.body)
                image_uuid = msg["image_uuid"]
                with jobs_lock:
                    if image_uuid in active_jobs:
                        active_jobs[image_uuid].set()
        finally:
            self._conn.ack(msg_id, sub_id)


def main() -> None:
    """Start the CLIP scorer process.

    Connects to Artemis via STOMP, subscribes to scorer.requests and
    scorer.events with durable subscriptions, and blocks indefinitely.
    """
    conn = stomp.Connection(host_and_ports=[(_STOMP_HOST, _STOMP_PORT)])
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER, _STOMP_PASS, wait=True,
        headers={"client-id": "clip-scorer"},
    )
    conn.subscribe(
        destination="/topic/scorer.requests",
        id=_SUB_REQUESTS,
        ack="client-individual",
        headers={"durable-subscription-name": "scorer.clip.requests"},
    )
    conn.subscribe(
        destination="/topic/scorer.events",
        id=_SUB_EVENTS,
        ack="client-individual",
        headers={"durable-subscription-name": "scorer.clip.events"},
    )
    print("CLIP scorer ready")
    while conn.is_connected():
        time.sleep(1)


if __name__ == "__main__":
    main()
