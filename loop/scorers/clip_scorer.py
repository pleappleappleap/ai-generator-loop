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
import math
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import open_clip
import stomp
import torch
from PIL import Image

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
_SUB_EVENTS = "2"

_disconnected = threading.Event()


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
        prompt_embedding = text_features.squeeze().tolist()
    return {
        "clip_score": similarity,
        "image_embedding": image_embedding,
        "prompt_embedding": prompt_embedding,
    }


def score_worker(
    image_uuid: str,
    image_path: str,
    prompt: str,
    cancel_event: threading.Event,
    conn: stomp.Connection,
    msg_id: str,
    sub_id: str,
) -> None:
    """Score an image on a worker thread with cancellation support.

    Wraps processing in a STOMP transaction so the ACK and result SEND
    are committed atomically. On cancellation, commits the ACK without
    sending a result. On unexpected failure, aborts for redelivery.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        prompt: Text prompt to compare against the image.
        cancel_event: threading.Event set by the cancel handler if the
            aggregator requests cancellation for this image_uuid.
        conn: STOMP connection for publishing the result (thread-safe).
        msg_id: STOMP message-id header of the request being processed.
        sub_id: STOMP subscription header of the request being processed.
    """
    tx_id = conn.begin()
    try:
        if cancel_event.is_set():
            conn.ack(msg_id, sub_id, transaction=tx_id)
            conn.commit(tx_id)
            return
        image = preprocess(Image.open(image_path)).unsqueeze(0).to(_device)
        text = tokenizer([prompt]).to(_device)
        if cancel_event.is_set():
            conn.ack(msg_id, sub_id, transaction=tx_id)
            conn.commit(tx_id)
            return
        with torch.no_grad():
            image_features = model.encode_image(image)
            if cancel_event.is_set():
                conn.ack(msg_id, sub_id, transaction=tx_id)
                conn.commit(tx_id)
                return
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = model.encode_text(text)
            if cancel_event.is_set():
                conn.ack(msg_id, sub_id, transaction=tx_id)
                conn.commit(tx_id)
                return
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            similarity = (image_features @ text_features.T).item()
            image_embedding = image_features.squeeze().tolist()
            prompt_embedding = text_features.squeeze().tolist()
        if math.isnan(similarity) or math.isinf(similarity):
            similarity = 0.0
        similarity = max(-1.0, min(1.0, similarity))
        result = {
            "image_uuid": image_uuid,
            "clip_score": similarity,
            "image_embedding": image_embedding,
            "prompt_embedding": prompt_embedding,
        }
        conn.send(
            destination="/queue/aggregator.clip.queue",
            body=json.dumps(result),
            headers={"persistent": "true"},
            transaction=tx_id,
        )
        conn.ack(msg_id, sub_id, transaction=tx_id)
        conn.commit(tx_id)
    except Exception as exc:
        print(f"[clip_scorer] ERROR scoring {image_uuid}: {exc}", file=sys.stderr)
        try:
            conn.abort(tx_id)
        except Exception:
            pass
    finally:
        with jobs_lock:
            active_jobs.pop(image_uuid, None)


class _Listener(stomp.ConnectionListener):
    """STOMP message listener for the CLIP scorer."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[clip_scorer] STOMP error: {frame.body}", file=sys.stderr)

    def on_disconnected(self) -> None:
        print("[clip_scorer] STOMP disconnected — exiting", file=sys.stderr)
        _disconnected.set()

    def on_message(self, frame: stomp.utils.Frame) -> None:
        sub_id = frame.headers.get("subscription", "")
        msg_id = frame.headers.get("message-id", "")
        if sub_id == _SUB_REQUESTS:
            # score_worker owns the transaction and acks within it.
            try:
                request = json.loads(frame.body)
                image_uuid = request["image_uuid"]
                cancel_event = threading.Event()
                with jobs_lock:
                    active_jobs[image_uuid] = cancel_event
                t = threading.Thread(
                    target=score_worker,
                    args=(
                        image_uuid,
                        request["image_path"],
                        request["prompt"],
                        cancel_event,
                        self._conn,
                        msg_id,
                        sub_id,
                    ),
                    daemon=True,
                )
                t.start()
            except Exception as exc:
                print(f"[clip_scorer] failed to dispatch {msg_id}: {exc}", file=sys.stderr)
                self._conn.ack(msg_id, sub_id)
        elif sub_id == _SUB_EVENTS:
            try:
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
    conn = stomp.Connection(
        host_and_ports=[(_STOMP_HOST, _STOMP_PORT)],
        heartbeats=(10000, 10000),
    )
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER,
        _STOMP_PASS,
        wait=True,
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
    _disconnected.wait()
    sys.exit(1)


if __name__ == "__main__":
    main()
