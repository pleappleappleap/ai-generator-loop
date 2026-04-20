"""Python scorer aggregator.

Note: The production aggregator is the Rust binary at
scorers/aggregator/. This Python implementation is retained as a
reference implementation and for environments where the Rust binary
is unavailable.

Subscribes to all three scorer result queues via the scorer.results
topic exchange. Accumulates results per image in Redis using the
agg:session:<image_uuid> key namespace. Applies cascade threshold
logic as results arrive — publishing cancel events and rejected verdicts
on threshold failures without waiting for remaining scorers.

When all three scorer results arrive within threshold bounds, atomically
removes the session record via Redis GETDEL and emits a candidate verdict
to the scorer.result queue.

The GETDEL operation provides race condition safety in scale-out
deployments where multiple aggregator instances may process results
for the same image concurrently.

Exchange subscriptions:
  scorer.results [topic] clip.*      → on_clip_result (via aggregator.clip.queue)
  scorer.results [topic] artifact.*  → on_artifact_result (via aggregator.artifact.queue)
  scorer.results [topic] vlm.*       → on_vlm_result (via aggregator.vlm.queue)

Exchange publications:
  scorer.events [topic] routing key: cancel.<image_uuid>
  scorer.result [queue] verdict messages
"""

import json
import sys
from pathlib import Path
from typing import Any

import pika
import pika.channel
import pika.spec
import redis as redis_lib

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

CLIP_THRESHOLD: float = _cfg.thresholds["clip"]
"""Minimum acceptable CLIP similarity score. Images scoring below this
value are rejected before artifact and VLM scoring."""

ARTIFACT_THRESHOLD: float = _cfg.thresholds["artifact"]
"""Maximum acceptable AI artifact confidence. Images scoring above this
value are rejected before VLM scoring."""

SCORE_TIMEOUT: int = _cfg.thresholds["score_timeout_secs"]
"""Redis TTL in seconds for in-flight session records. Sessions not
resolved within this window are expired by Redis automatically."""

r = redis_lib.Redis.from_url(
    _cfg.broker["redis_url"], decode_responses=True
)


def setup_exchanges(channel: pika.channel.Channel) -> None:
    """Declare all RabbitMQ exchanges required by this process.

    Args:
        channel: An open pika channel.
    """
    channel.exchange_declare(
        exchange="scorer.results", exchange_type="topic", durable=True
    )
    channel.exchange_declare(
        exchange="scorer.events", exchange_type="topic", durable=True
    )


def publish_cancel(channel: pika.channel.Channel, image_uuid: str) -> None:
    """Publish a cancel event to the scorer.events topic exchange.

    Signals all active scorers to abandon in-progress work for this
    image. Scorers check their cancel flag at inference checkpoints
    and exit without publishing results.

    Args:
        channel: An open pika channel.
        image_uuid: UUID of the image whose scoring should be cancelled.
    """
    channel.basic_publish(
        exchange="scorer.events",
        routing_key=f"cancel.{image_uuid}",
        body=json.dumps({"image_uuid": image_uuid}),
    )


def publish_verdict(
    channel: pika.channel.Channel,
    image_uuid: str,
    session: dict[str, Any],
    verdict: str,
    reason: str | None = None,
) -> None:
    """Publish a verdict to the scorer.result queue.

    Args:
        channel: An open pika channel.
        image_uuid: UUID of the scored image.
        session: Accumulated session dict containing all available
            scorer results at the time of verdict.
        verdict: "candidate" or "rejected".
        reason: "clip_threshold" or "artifact_threshold" for rejections,
            None for candidates.
    """
    output: dict[str, Any] = {
        "image_uuid":      image_uuid,
        "verdict":         verdict,
        "scores":          session,
        "prompt":          session.get("prompt", ""),
        "session_uuid":    session.get("session_uuid", ""),
        "workflow_path":   session.get("workflow_path", ""),
        "sequence_number": session.get("sequence_number", 0),
    }
    if reason:
        output["reason"] = reason
    channel.basic_publish(
        exchange="",
        routing_key="scorer.result",
        body=json.dumps(output),
    )


def try_aggregate(
    channel: pika.channel.Channel,
    image_uuid: str,
    session: dict[str, Any],
) -> None:
    """Attempt to emit a candidate verdict if all scores have arrived.

    Checks whether all three scorer results are present in the session
    dict. If so, atomically removes the session record via GETDEL and
    publishes a candidate verdict.

    The GETDEL ensures only one aggregator instance emits a verdict
    in scale-out deployments.

    Args:
        channel: An open pika channel.
        image_uuid: UUID of the image being aggregated.
        session: Current session dict, which may or may not have all
            three scorer results populated.
    """
    if not all([session.get("clip"), session.get("artifact"), session.get("vlm")]):
        return
    key = f"agg:session:{image_uuid}"
    raw = r.getdel(key)
    if raw:
        final_session = json.loads(raw)
        publish_verdict(channel, image_uuid, final_session, "candidate")


def on_clip_result(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a CLIP scorer result from scorer.results.

    Updates the Redis session record with the CLIP score. If the score
    falls below CLIP_THRESHOLD, atomically removes the session, publishes
    a cancel event, and emits a rejected verdict. Otherwise stores the
    result and calls try_aggregate.

    If the session key is not found in Redis, the session has expired
    or was already processed. The message is silently acked.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties (unused).
        body: JSON-encoded bytes containing image_uuid, clip_score,
            and image_embedding.
    """
    result = json.loads(body)
    image_uuid = result["image_uuid"]
    key = f"agg:session:{image_uuid}"
    raw = r.get(key)
    if not raw:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    session = json.loads(raw)
    session["clip"] = result
    if result["clip_score"] < CLIP_THRESHOLD:
        raw = r.getdel(key)
        if raw:
            publish_cancel(ch, image_uuid)
            publish_verdict(ch, image_uuid, session, "rejected", "clip_threshold")
    else:
        r.setex(key, SCORE_TIMEOUT, json.dumps(session))
        try_aggregate(ch, image_uuid, session)
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_artifact_result(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle an artifact scorer result from scorer.results.

    Updates the Redis session record with the artifact confidence score.
    If the confidence exceeds ARTIFACT_THRESHOLD, atomically removes the
    session, publishes a cancel event, and emits a rejected verdict.
    Otherwise stores the result and calls try_aggregate.

    If the session key is not found in Redis, the session has expired
    or was already processed. The message is silently acked.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties (unused).
        body: JSON-encoded bytes containing image_uuid and ai_confidence.
    """
    result = json.loads(body)
    image_uuid = result["image_uuid"]
    key = f"agg:session:{image_uuid}"
    raw = r.get(key)
    if not raw:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    session = json.loads(raw)
    session["artifact"] = result
    if result["ai_confidence"] > ARTIFACT_THRESHOLD:
        raw = r.getdel(key)
        if raw:
            publish_cancel(ch, image_uuid)
            publish_verdict(ch, image_uuid, session, "rejected", "artifact_threshold")
    else:
        r.setex(key, SCORE_TIMEOUT, json.dumps(session))
        try_aggregate(ch, image_uuid, session)
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_vlm_result(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a VLM scorer result from scorer.results.

    Updates the Redis session record with the VLM scores and calls
    try_aggregate. The VLM result never triggers a rejection directly
    — VLM scores are interpreted by the tactical LLM after the verdict
    is emitted.

    If the session key is not found in Redis, the session has expired
    or was already processed. The message is silently acked.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties (unused).
        body: JSON-encoded bytes containing image_uuid and VLM scores.
    """
    result = json.loads(body)
    image_uuid = result["image_uuid"]
    key = f"agg:session:{image_uuid}"
    raw = r.get(key)
    if not raw:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    session = json.loads(raw)
    session["vlm"] = result
    r.setex(key, SCORE_TIMEOUT, json.dumps(session))
    try_aggregate(ch, image_uuid, session)
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    """Start the scorer aggregator process.

    Connects to RabbitMQ, declares exchanges and queues, binds result
    queues to the scorer.results topic exchange, and begins consuming.
    Blocks indefinitely. This is the process entry point.
    """
    connection = pika.BlockingConnection(
        pika.URLParameters(_cfg.broker["rabbitmq_url"])
    )
    channel = connection.channel()
    setup_exchanges(channel)

    channel.queue_declare(queue="scorer.result", durable=True)

    for queue, binding in [
        ("aggregator.clip.queue", "clip.*"),
        ("aggregator.artifact.queue", "artifact.*"),
        ("aggregator.vlm.queue", "vlm.*"),
    ]:
        channel.queue_declare(queue=queue, durable=True)
        channel.queue_bind(
            exchange="scorer.results",
            queue=queue,
            routing_key=binding,
        )

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(
        queue="aggregator.clip.queue", on_message_callback=on_clip_result
    )
    channel.basic_consume(
        queue="aggregator.artifact.queue", on_message_callback=on_artifact_result
    )
    channel.basic_consume(
        queue="aggregator.vlm.queue", on_message_callback=on_vlm_result
    )
    print("Scorer aggregator ready")
    channel.start_consuming()


if __name__ == "__main__":
    main()
