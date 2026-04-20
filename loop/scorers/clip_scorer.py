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

Exchange subscriptions:
  scorer.requests [topic] score.*   → on_score_request (via scorer.clip.requests)
  scorer.events   [topic] cancel.*  → on_cancel (via scorer.clip.cancel)

Exchange publications:
  scorer.results  [topic] routing key: clip.<image_uuid>
"""

import json
import sys
import threading
from pathlib import Path
from typing import Any

import open_clip
import pika
import pika.channel
import pika.spec
import torch
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

model, _, preprocess = open_clip.create_model_and_transforms(
    _cfg.models["clip"]["name"],
    pretrained=_cfg.models["clip"]["pretrained"],
)
tokenizer = open_clip.get_tokenizer(_cfg.models["clip"]["name"])

# Select GPU device from config. On macOS with an eGPU, MPS uses
# Metal's system-default device (typically the eGPU when connected).
_backend = _cfg.gpu["backend"]
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


def score(image_path: str, prompt: str) -> dict[str, Any]:
    """Compute CLIP similarity score and image embedding synchronously.

    Convenience function for testing and direct use. Does not support
    cancellation. For pipeline use, prefer score_worker which runs on
    a thread with cancel support.

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
    channel: pika.channel.Channel,
) -> None:
    """Score an image on a worker thread with cancellation support.

    Checks the cancel_event between each major inference operation.
    If cancelled, exits without publishing a result. Removes the job
    from active_jobs on exit regardless of outcome.

    Publishes to scorer.results with routing key clip.<image_uuid>
    on successful completion.

    Args:
        image_uuid: UUID of the image being scored.
        image_path: Filesystem path or URL to the image file.
        prompt: Text prompt to compare against the image.
        cancel_event: threading.Event set by on_cancel if the aggregator
            requests cancellation for this image_uuid.
        channel: Pika channel for publishing the result.
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
            "image_uuid": image_uuid,
            "clip_score": similarity,
            "image_embedding": image_embedding,
        }
        channel.basic_publish(
            exchange="scorer.results",
            routing_key=f"clip.{image_uuid}",
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
    and immediately acks the message. The worker thread runs
    asynchronously and publishes its result independently.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties (unused).
        body: JSON-encoded bytes. Expected keys:
            image_uuid (str): UUID of the image to score.
            image_path (str): Path or URL to the image file.
            prompt (str): Text prompt for similarity comparison.
    """
    request = json.loads(body)
    image_uuid = request["image_uuid"]
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    t = threading.Thread(
        target=score_worker,
        args=(image_uuid, request["image_path"], request["prompt"], cancel_event, ch),
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
    is currently in flight. The worker thread will check the flag at
    the next cancellation checkpoint and exit without publishing.

    If no active job exists for the image_uuid, the message is silently
    acked — the job may have completed before the cancel arrived.

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
    """Start the CLIP scorer process.

    Connects to RabbitMQ, declares exchanges and queues, binds queues
    to exchanges, and begins consuming scoring and cancel requests.
    Blocks indefinitely. This is the process entry point.
    """
    connection = pika.BlockingConnection(pika.URLParameters(_cfg.broker["rabbitmq_url"]))
    channel = connection.channel()
    setup_exchanges(channel)

    channel.queue_declare(queue="scorer.clip.requests", durable=True)
    channel.queue_bind(
        exchange="scorer.requests",
        queue="scorer.clip.requests",
        routing_key="score.*",
    )
    channel.queue_declare(queue="scorer.clip.cancel", durable=True)
    channel.queue_bind(
        exchange="scorer.events",
        queue="scorer.clip.cancel",
        routing_key="cancel.*",
    )
    channel.basic_qos(prefetch_count=4)
    channel.basic_consume(
        queue="scorer.clip.requests", on_message_callback=on_score_request
    )
    channel.basic_consume(
        queue="scorer.clip.cancel", on_message_callback=on_cancel
    )
    print("CLIP scorer ready")
    channel.start_consuming()


if __name__ == "__main__":
    main()
