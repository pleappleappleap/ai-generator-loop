"""AI artifact detection scorer.

Classifies generated images using the umm-maybe/AI-image-detector model
to estimate the probability that an image is AI-generated. Lower confidence
indicates more photorealistic output.

The model is loaded once at process startup and held resident. Scoring
requests are processed on worker threads with cancellation support.
The classifier is fast enough that cancellation is checked only at the
start and end of inference rather than mid-operation.

Exchange subscriptions:
  scorer.requests [topic] score.*   → on_score_request (via scorer.artifact.requests)
  scorer.events   [topic] cancel.*  → on_cancel (via scorer.artifact.cancel)

Exchange publications:
  scorer.results  [topic] routing key: artifact.<image_uuid>
"""

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pika
import pika.channel
import pika.spec
from transformers import pipeline

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

detector = pipeline(
    "image-classification",
    model=_cfg.models["artifact_detector"]["dir"],
)

active_jobs: dict[str, threading.Event] = {}
"""Map of image_uuid to cancel Event for in-flight scoring jobs."""

jobs_lock = threading.Lock()
"""Lock protecting active_jobs for concurrent access."""


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


def score(image_path: str) -> dict[str, Any]:
    """Compute AI artifact confidence score synchronously.

    Convenience function for testing and direct use. Does not support
    cancellation. For pipeline use, prefer score_worker.

    Args:
        image_path: Filesystem path or URL to the image file.

    Returns:
        Dict with key:
            ai_confidence (float): Probability the image is AI-generated.
                Range 0.0–1.0. Lower values indicate more photorealistic
                output. Returns 0.0 if the "artificial" label is absent
                from the classifier output.
    """
    results = detector(image_path)
    ai_confidence = next(
        (r["score"] for r in results if r["label"] == "artificial"), 0.0
    )
    return {"ai_confidence": ai_confidence}


def score_worker(
    image_uuid: str,
    image_path: str,
    cancel_event: threading.Event,
    channel: pika.channel.Channel,
) -> None:
    """Score an image on a worker thread with cancellation support.

    Checks the cancel_event before and after inference. If cancelled,
    exits without publishing a result. Removes the job from active_jobs
    on exit regardless of outcome.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        cancel_event: threading.Event set by on_cancel if the aggregator
            requests cancellation for this image_uuid.
        channel: Pika channel for publishing the result.
    """
    try:
        if cancel_event.is_set():
            return
        results = detector(image_path)
        if cancel_event.is_set():
            return
        ai_confidence = next(
            (r["score"] for r in results if r["label"] == "artificial"), 0.0
        )
        result = {
            "image_uuid": image_uuid,
            "ai_confidence": ai_confidence,
        }
        channel.basic_publish(
            exchange="scorer.results",
            routing_key=f"artifact.{image_uuid}",
            body=json.dumps(result),
        )
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
    """
    request = json.loads(body)
    image_uuid = request["image_uuid"]
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    t = threading.Thread(
        target=score_worker,
        args=(image_uuid, request["image_path"], cancel_event, ch),
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
    is currently in flight.

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
    """Start the artifact scorer process.

    Connects to RabbitMQ, declares exchanges and queues, binds queues
    to exchanges, and begins consuming. Blocks indefinitely.
    """
    connection = pika.BlockingConnection(pika.URLParameters(_cfg.broker["rabbitmq_url"]))
    channel = connection.channel()
    setup_exchanges(channel)

    channel.queue_declare(queue="scorer.artifact.requests", durable=True)
    channel.queue_bind(
        exchange="scorer.requests",
        queue="scorer.artifact.requests",
        routing_key="score.*",
    )
    channel.queue_declare(queue="scorer.artifact.cancel", durable=True)
    channel.queue_bind(
        exchange="scorer.events",
        queue="scorer.artifact.cancel",
        routing_key="cancel.*",
    )
    channel.basic_qos(prefetch_count=2)
    channel.basic_consume(
        queue="scorer.artifact.requests", on_message_callback=on_score_request
    )
    channel.basic_consume(
        queue="scorer.artifact.cancel", on_message_callback=on_cancel
    )
    print("Artifact scorer ready")
    channel.start_consuming()


if __name__ == "__main__":
    main()
