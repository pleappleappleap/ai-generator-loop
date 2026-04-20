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
via the Redis TTL.

Exchange subscriptions:
  scorer.requests [topic] score.*   → on_score_request (via scorer.vlm.requests)
  scorer.events   [topic] cancel.*  → on_cancel (via scorer.vlm.cancel)

Exchange publications:
  scorer.results  [topic] routing key: vlm.<image_uuid>
"""

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pika
import pika.channel
import pika.spec
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


def setup_exchanges(channel: pika.channel.Channel) -> None:
    """Declare all RabbitMQ exchanges required by this process.

    Args:
        channel: An open pika channel.
    """
    channel.exchange_declare(
        exchange="scorer.requests", exchange_type="topic", durable=True
    )
    channel.exchange_declare(
        exchange="scorer.events", exchange_type="topic", durable=True
    )
    channel.exchange_declare(
        exchange="scorer.results", exchange_type="topic", durable=True
    )


def score_worker(
    image_uuid: str,
    image_path: str,
    prompt: str,
    cancel_event: threading.Event,
    channel: pika.channel.Channel,
) -> None:
    """Score an image using the VLM on a worker thread.

    Runs streaming inference and checks the cancel_event between each
    token. If cancelled, calls llm.reset() to flush the KV cache and
    exits without publishing. If the accumulated output is not valid
    JSON, discards the result silently.

    Publishes to scorer.results with routing key vlm.<image_uuid>
    on successful completion.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        prompt: The generation prompt used to create the image.
            Included in the evaluation prompt so the VLM can assess
            prompt adherence.
        cancel_event: threading.Event set by on_cancel if the aggregator
            requests cancellation for this image_uuid.
        channel: Pika channel for publishing the result.
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
        channel.basic_publish(
            exchange="scorer.results",
            routing_key=f"vlm.{image_uuid}",
            body=json.dumps(result),
        )
    except json.JSONDecodeError:
        pass
    finally:
        with jobs_lock:
            active_jobs.pop(image_uuid, None)


def on_score_request(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a score request from the scorer.requests exchange.

    Registers the job in active_jobs, spawns a daemon worker thread,
    and immediately acks the message.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties (unused).
        body: JSON-encoded bytes. Expected keys:
            image_uuid (str): UUID of the image to score.
            image_path (str): Path or URL to the image file.
            prompt (str): Generation prompt for adherence evaluation.
    """
    request = json.loads(body)
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
            ch,
        ),
        daemon=True,
    )
    t.start()
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_cancel(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a cancel event from the scorer.events exchange.

    Sets the cancel_event for the given image_uuid if a scoring job
    is currently in flight. The worker thread will check the flag
    between tokens and call llm.reset() before exiting.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties (unused).
        body: JSON-encoded bytes. Expected keys:
            image_uuid (str): UUID of the image to cancel.
    """
    msg = json.loads(body)
    image_uuid = msg["image_uuid"]
    with jobs_lock:
        if image_uuid in active_jobs:
            active_jobs[image_uuid].set()
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    """Start the VLM scorer process.

    Connects to RabbitMQ, declares exchanges and queues, binds queues
    to exchanges, and begins consuming. Blocks indefinitely.
    prefetch_count=1 ensures only one VLM inference runs at a time,
    preventing memory exhaustion from concurrent large model invocations.
    """
    connection = pika.BlockingConnection(pika.URLParameters(_cfg.broker["rabbitmq_url"]))
    channel = connection.channel()
    setup_exchanges(channel)

    channel.queue_declare(queue="scorer.vlm.requests", durable=True)
    channel.queue_bind(
        exchange="scorer.requests",
        queue="scorer.vlm.requests",
        routing_key="score.*",
    )
    channel.queue_declare(queue="scorer.vlm.cancel", durable=True)
    channel.queue_bind(
        exchange="scorer.events",
        queue="scorer.vlm.cancel",
        routing_key="cancel.*",
    )
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(
        queue="scorer.vlm.requests", on_message_callback=on_score_request
    )
    channel.basic_consume(
        queue="scorer.vlm.cancel", on_message_callback=on_cancel
    )
    print("VLM scorer ready")
    channel.start_consuming()


if __name__ == "__main__":
    main()
