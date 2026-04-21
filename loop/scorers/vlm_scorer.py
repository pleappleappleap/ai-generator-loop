"""VLM holistic image evaluation scorer.

Uses Qwen2.5-VL-7B-Instruct at Q5_K_M quantization via llama-cpp-python
to evaluate generated images across five quality dimensions: photorealism,
anatomical coherence, character interaction plausibility, lighting
consistency, and prompt adherence.

The model is loaded once at process startup and held resident. Scoring
uses streaming inference, which allows clean cancellation between tokens.
When a cancel event is set, the worker calls llm.reset() to flush the
KV cache before exiting, leaving the model in a clean state for the
next inference.

If the VLM produces malformed JSON output, the result is silently
discarded without publishing — the aggregator will time out the session
via the scorer_session expires_at column.

Address subscriptions (STOMP / Artemis):
  /topic/scorer.requests  (durable: scorer.vlm.requests)  → score requests
  /topic/scorer.events    (durable: scorer.vlm.events)    → cancel events

Address publications (STOMP / Artemis):
  /queue/aggregator.vlm.queue  → vlm score results
"""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import stomp
from llama_cpp import Llama

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()
_vlm_cfg = _cfg.models["vlm"]

llm = Llama(
    model_path=str(Path(_vlm_cfg["dir"]) / _vlm_cfg["filename"]),
    n_ctx=_vlm_cfg["context_length"],
    n_gpu_layers=_vlm_cfg["n_gpu_layers"],
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
_SUB_EVENTS   = "2"

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
"""Evaluation prompt template. Uses double braces to escape the JSON
structure for Python's str.format()."""


def score_worker(
    image_uuid: str,
    image_path: str,
    prompt: str,
    cancel_event: threading.Event,
    conn: stomp.Connection,
) -> None:
    """Score an image using the VLM on a worker thread.

    Runs streaming inference and checks the cancel_event between each
    token. If cancelled, calls llm.reset() to flush the KV cache and
    exits without publishing. If the accumulated output is not valid
    JSON, discards the result silently.

    Publishes to /queue/aggregator.vlm.queue on successful completion.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        prompt: The generation prompt used to create the image.
        cancel_event: threading.Event set by the cancel handler.
        conn: STOMP connection for publishing the result (thread-safe).
    """
    try:
        accumulated = ""
        for token in llm(
            EVAL_PROMPT.format(prompt=prompt),
            stream=True,
            max_tokens=512,
        ):
            if cancel_event.is_set():
                llm.reset()
                return
            accumulated += token["choices"][0]["text"]

        result: dict[str, Any] = json.loads(accumulated)
        result["image_uuid"] = image_uuid
        conn.send(
            destination="/queue/aggregator.vlm.queue",
            body=json.dumps(result),
            headers={"persistent": "true"},
        )
    except json.JSONDecodeError:
        pass
    finally:
        with jobs_lock:
            active_jobs.pop(image_uuid, None)


class _Listener(stomp.ConnectionListener):
    """STOMP message listener for the VLM scorer."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[vlm_scorer] STOMP error: {frame.body}", file=sys.stderr)

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
    """Start the VLM scorer process.

    Connects to Artemis via STOMP, subscribes to scorer.requests and
    scorer.events with durable subscriptions, and blocks indefinitely.
    prefetch_count=1 equivalent: the listener thread blocks while the
    VLM worker runs (only one active subscription message at a time via
    ack='client-individual').
    """
    conn = stomp.Connection(host_and_ports=[(_STOMP_HOST, _STOMP_PORT)])
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER, _STOMP_PASS, wait=True,
        headers={"client-id": "vlm-scorer"},
    )
    conn.subscribe(
        destination="/topic/scorer.requests",
        id=_SUB_REQUESTS,
        ack="client-individual",
        headers={"durable-subscription-name": "scorer.vlm.requests"},
    )
    conn.subscribe(
        destination="/topic/scorer.events",
        id=_SUB_EVENTS,
        ack="client-individual",
        headers={"durable-subscription-name": "scorer.vlm.events"},
    )
    print("VLM scorer ready")
    while conn.is_connected():
        time.sleep(1)


if __name__ == "__main__":
    main()
