"""AI artifact detection scorer.

Classifies generated images using the umm-maybe/AI-image-detector model
to estimate the probability that an image is AI-generated. Lower confidence
indicates more photorealistic output.

The model is loaded once at process startup and held resident. Scoring
requests are processed on worker threads with cancellation support.
The classifier is fast enough that cancellation is checked only at the
start and end of inference rather than mid-operation.

Address subscriptions (STOMP / Artemis):
  /topic/scorer.requests  (durable: scorer.artifact.requests)  → score requests
  /topic/scorer.events    (durable: scorer.artifact.events)    → cancel events

Address publications (STOMP / Artemis):
  /queue/aggregator.artifact.queue  → artifact score results
"""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import stomp
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


def score(image_path: str) -> dict[str, Any]:
    """Compute AI artifact confidence score synchronously.

    Args:
        image_path: Filesystem path or URL to the image file.

    Returns:
        Dict with key:
            ai_confidence (float): Probability the image is AI-generated.
                Range 0.0–1.0. Lower values indicate more photorealistic
                output.
    """
    results = detector(image_path)
    ai_confidence = next((r["score"] for r in results if r["label"] == "artificial"), 0.0)
    return {"ai_confidence": ai_confidence}


def score_worker(
    image_uuid: str,
    image_path: str,
    cancel_event: threading.Event,
    conn: stomp.Connection,
) -> None:
    """Score an image on a worker thread with cancellation support.

    Checks the cancel_event before and after inference. If cancelled,
    exits without publishing a result. Removes the job from active_jobs
    on exit regardless of outcome.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        cancel_event: threading.Event set by the cancel handler.
        conn: STOMP connection for publishing the result (thread-safe).
    """
    try:
        if cancel_event.is_set():
            return
        results = detector(image_path)
        if cancel_event.is_set():
            return
        ai_confidence = next((r["score"] for r in results if r["label"] == "artificial"), 0.0)
        result = {
            "image_uuid": image_uuid,
            "ai_confidence": ai_confidence,
        }
        conn.send(
            destination="/queue/aggregator.artifact.queue",
            body=json.dumps(result),
            headers={"persistent": "true"},
        )
    finally:
        with jobs_lock:
            active_jobs.pop(image_uuid, None)


class _Listener(stomp.ConnectionListener):
    """STOMP message listener for the artifact scorer."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[artifact_scorer] STOMP error: {frame.body}", file=sys.stderr)

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
                    args=(image_uuid, request["image_path"], cancel_event, self._conn),
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
    """Start the artifact scorer process.

    Connects to Artemis via STOMP, subscribes to scorer.requests and
    scorer.events with durable subscriptions, and blocks indefinitely.
    """
    conn = stomp.Connection(host_and_ports=[(_STOMP_HOST, _STOMP_PORT)])
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER,
        _STOMP_PASS,
        wait=True,
        headers={"client-id": "artifact-scorer"},
    )
    conn.subscribe(
        destination="/topic/scorer.requests",
        id=_SUB_REQUESTS,
        ack="client-individual",
        headers={"durable-subscription-name": "scorer.artifact.requests"},
    )
    conn.subscribe(
        destination="/topic/scorer.events",
        id=_SUB_EVENTS,
        ack="client-individual",
        headers={"durable-subscription-name": "scorer.artifact.events"},
    )
    print("Artifact scorer ready")
    while conn.is_connected():
        time.sleep(1)


if __name__ == "__main__":
    main()
