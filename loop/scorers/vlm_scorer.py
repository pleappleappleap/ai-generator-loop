"""VLM holistic image evaluation scorer.

Uses Qwen3-VL-8B-Instruct-abliterated at Q8_0 quantization via
llama-cpp-python to evaluate generated images across five quality
dimensions: photorealism, anatomical coherence, character interaction
plausibility, lighting consistency, and prompt adherence.

The model and its multimodal projector (mmproj) are loaded once at
process startup and held resident. Scoring uses streaming inference,
which allows clean cancellation between tokens. When a cancel event is
set, the worker exits without publishing, leaving the model in a clean
state for the next inference.

If the VLM produces malformed JSON output, the result is silently
discarded without publishing — the aggregator will time out the session
via the scorer_session expires_at column.

Address subscriptions (STOMP / Artemis):
  /topic/scorer.requests  (durable: scorer.vlm.requests)  → score requests
  /topic/scorer.events    (durable: scorer.vlm.events)    → cancel events

Address publications (STOMP / Artemis):
  /queue/aggregator.vlm.queue  → vlm score results
"""

import base64
import json
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import stomp
from llama_cpp import Llama
from llama_cpp.llama_chat_format import Qwen25VLChatHandler

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()
_vlm_cfg = _cfg.models["vlm"]
_vlm_compute = _cfg.compute["vlm_scorer"]

llm: Llama | None = None

active_jobs: dict[str, threading.Event] = {}
jobs_lock = threading.Lock()

_u = urlparse(_cfg.broker["stomp_url"])
_STOMP_HOST: str = _u.hostname or "localhost"
_STOMP_PORT: int = _u.port or 61613
_STOMP_USER: str = _u.username or ""
_STOMP_PASS: str = _u.password or ""

_SUB_REQUESTS = "1"
_SUB_EVENTS = "2"

_disconnected = threading.Event()

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


def score_worker(
    image_uuid: str,
    image_path: str,
    prompt: str,
    cancel_event: threading.Event,
    conn: stomp.Connection,
    msg_id: str,
    sub_id: str,
) -> None:
    """Score an image using the VLM on a worker thread.

    Wraps processing in a STOMP transaction so the ACK and result SEND
    are committed atomically. Soft failures (unreadable image, malformed
    VLM output, cancellation) commit the ACK without a result to avoid
    redelivery loops. Unexpected errors abort for redelivery.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path to the image file.
        prompt: The generation prompt used to create the image.
        cancel_event: threading.Event set by the cancel handler.
        conn: STOMP connection for publishing the result (thread-safe).
        msg_id: STOMP message-id header of the request being processed.
        sub_id: STOMP subscription header of the request being processed.
    """
    tx_id = conn.begin()
    accumulated = ""
    try:
        if cancel_event.is_set():
            conn.ack(msg_id, sub_id, transaction=tx_id)
            conn.commit(tx_id)
            return

        try:
            with open(image_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode()
            ext = Path(image_path).suffix.lower().lstrip(".")
            mime = "jpeg" if ext in ("jpg", "jpeg") else ext or "png"
            image_url = f"data:image/{mime};base64,{image_b64}"
        except OSError as exc:
            print(f"[vlm_scorer] cannot read image {image_path}: {exc}", file=sys.stderr)
            conn.ack(msg_id, sub_id, transaction=tx_id)
            conn.commit(tx_id)
            return

        stream = llm.create_chat_completion(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": EVAL_PROMPT.format(prompt=prompt)},
                    ],
                }
            ],
            max_tokens=512,
            stream=True,
        )
        for chunk in stream:
            if cancel_event.is_set():
                conn.ack(msg_id, sub_id, transaction=tx_id)
                conn.commit(tx_id)
                return
            delta = chunk["choices"][0].get("delta", {}).get("content") or ""
            accumulated += delta

        try:
            result: dict[str, Any] = json.loads(accumulated)
        except json.JSONDecodeError as exc:
            print(
                f"[vlm_scorer] JSON parse failed for {image_uuid}: {exc}  "
                f"raw output: {accumulated[:200]!r}",
                file=sys.stderr,
            )
            conn.ack(msg_id, sub_id, transaction=tx_id)
            conn.commit(tx_id)
            return

        score_fields = (
            "photorealism", "anatomical_coherence", "interaction_plausibility",
            "lighting_consistency", "prompt_adherence",
        )
        for field in score_fields:
            if field in result and isinstance(result[field], (int, float)):
                result[field] = max(0.0, min(10.0, float(result[field])))
        result["image_uuid"] = image_uuid
        conn.send(
            destination="/queue/aggregator.vlm.queue",
            body=json.dumps(result),
            headers={"persistent": "true"},
            transaction=tx_id,
        )
        conn.ack(msg_id, sub_id, transaction=tx_id)
        conn.commit(tx_id)
    except Exception as exc:
        print(f"[vlm_scorer] ERROR scoring {image_uuid}: {exc}", file=sys.stderr)
        try:
            conn.abort(tx_id)
        except Exception:
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

    def on_disconnected(self) -> None:
        print("[vlm_scorer] STOMP disconnected — exiting", file=sys.stderr)
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
                print(f"[vlm_scorer] failed to dispatch {msg_id}: {exc}", file=sys.stderr)
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
    """Start the VLM scorer process.

    Loads the model and mmproj, connects to Artemis via STOMP, subscribes
    to scorer.requests and scorer.events with durable subscriptions, and
    blocks indefinitely.
    """
    global llm
    model_dir = Path(_vlm_cfg["dir"])
    chat_handler = Qwen25VLChatHandler(
        clip_model_path=str(model_dir / _vlm_cfg["mmproj_filename"])
    )
    llm = Llama(
        model_path=str(model_dir / _vlm_cfg["filename"]),
        chat_handler=chat_handler,
        n_ctx=_vlm_cfg["context_length"],
        n_gpu_layers=_vlm_compute["n_gpu_layers"],
        logits_all=True,
    )

    conn = stomp.Connection(
        host_and_ports=[(_STOMP_HOST, _STOMP_PORT)],
        heartbeats=(10000, 10000),
    )
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER,
        _STOMP_PASS,
        wait=True,
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
    print(f"  model:   {_vlm_cfg['filename']}")
    print(f"  mmproj:  {_vlm_cfg['mmproj_filename']}")
    _disconnected.wait()
    sys.exit(1)


if __name__ == "__main__":
    main()
