"""LanceDB write manager for the AI image generation pipeline.

Subscribes to scorer results, cancel events, and accepted image events
via RabbitMQ. Accumulates per-image scorer results in Redis under the
ldb:session:<image_uuid> key namespace, then writes complete records
to LanceDB when the full picture is available.

Write triggers:
  - cancel event: writes a rejected record with null image_path
  - loop.accepted event: writes a candidate record with image_path

This module lives at ~/ai-image/ rather than under loop/ or strategic-llm/
because it serves both subsystems. The loop writes generation records via
the message-driven path. The strategic LLM reads those records via direct
LanceDB queries using the schema defined in lancedb_schema.py.

Queue and exchange subscriptions:
  scorer.results [topic] clip.*      → lancedb.clip.queue
  scorer.results [topic] artifact.*  → lancedb.artifact.queue
  scorer.results [topic] vlm.*       → lancedb.vlm.queue
  scorer.events  [topic] cancel.*    → lancedb.cancel.queue
  loop.accepted  [topic] accepted.*  → lancedb.accepted.queue
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import lancedb
import open_clip
import pika
import pika.channel
import pika.spec
import redis as redis_lib
import torch

from lancedb_schema import CLIP_EMBEDDING_DIM, Loop, Session

sys.path.insert(0, str(Path(__file__).parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

SCORE_TIMEOUT: int = _cfg.thresholds["score_timeout_secs"] * 2
"""Redis TTL in seconds for in-flight session records.

Twice the aggregator TTL to account for the additional delay between
the aggregator emitting a verdict and the accepted or cancel event
arriving at the LanceDB manager.
"""

LANCEDB_PATH: str = _cfg.paths["lancedb"]
"""Filesystem path to the LanceDB storage directory."""

r = redis_lib.Redis.from_url(_cfg.broker["redis_url"], decode_responses=True)

_clip_cfg = _cfg.models["clip"]
_model, _, _preprocess = open_clip.create_model_and_transforms(
    _clip_cfg["name"], pretrained=_clip_cfg["pretrained"]
)
_tokenizer = open_clip.get_tokenizer(_clip_cfg["name"])


def get_db() -> lancedb.LanceDBConnection:
    """Return a connection to the LanceDB storage directory.

    Returns:
        A LanceDB connection to LANCEDB_PATH.
    """
    return lancedb.connect(LANCEDB_PATH)


def ensure_tables() -> None:
    """Create LanceDB tables if they do not already exist.

    Creates the sessions and loop tables using the schemas defined in
    lancedb_schema.py. Safe to call on every startup — existing tables
    are left unchanged.
    """
    db = get_db()
    if "sessions" not in db.table_names():
        db.create_table("sessions", schema=Session.to_arrow_schema())
    if "loop" not in db.table_names():
        db.create_table("loop", schema=Loop.to_arrow_schema())


def encode_prompt(prompt: str) -> list[float]:
    """Encode a text prompt as a normalized CLIP embedding.

    Uses the ViT-L-14 CLIP model loaded at module import time.
    The embedding is L2-normalized so cosine similarity can be
    computed as a dot product.

    Args:
        prompt: Natural language text to encode.

    Returns:
        A list of CLIP_EMBEDDING_DIM float values representing the
        normalized text embedding.
    """
    text = _tokenizer([prompt])
    with torch.no_grad():
        features = _model.encode_text(text)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze().tolist()


def write_loop_record(
    session: dict[str, Any],
    image_path: str | None = None,
) -> None:
    """Write a loop record to LanceDB.

    Constructs a Loop Pydantic model from the accumulated session dict
    and appends it to the loop table. Called on cancel (rejected) or
    accepted events.

    For rejected images, image_path is None and rejection_reason is set
    from the session dict. For accepted candidates, image_path is the
    filesystem path to the saved image file.

    Args:
        session: Dict containing accumulated scorer results and metadata.
            Expected keys: image_uuid, session_uuid, sequence_number,
            prompt, workflow_path, workflow_params, clip, artifact, vlm,
            verdict, rejection_reason.
        image_path: Absolute filesystem path to the saved image file,
            or None for rejected images.
    """
    db = get_db()
    table = db.open_table("loop")

    clip = session.get("clip", {})
    artifact = session.get("artifact", {})
    vlm = session.get("vlm", {})

    record = Loop(
        image_uuid=session["image_uuid"],
        session_uuid=session.get("session_uuid", ""),
        sequence_number=session.get("sequence_number", 0),
        created_at=datetime.utcnow().isoformat(),
        prompt=session.get("prompt", ""),
        prompt_embedding=encode_prompt(session.get("prompt", "")),
        image_embedding=clip.get("image_embedding", [0.0] * CLIP_EMBEDDING_DIM),
        workflow_path=session.get("workflow_path", ""),
        workflow_params=json.dumps(session.get("workflow_params", {})),
        clip_score=float(clip.get("clip_score", 0.0)),
        artifact_score=float(artifact.get("ai_confidence", 0.0)),
        vlm_photorealism=float(vlm.get("photorealism", 0.0)),
        vlm_anatomical=float(vlm.get("anatomical_coherence", 0.0)),
        vlm_interaction=float(vlm.get("interaction_plausibility", 0.0)),
        vlm_lighting=float(vlm.get("lighting_consistency", 0.0)),
        vlm_prompt=float(vlm.get("prompt_adherence", 0.0)),
        vlm_issues=json.dumps(vlm.get("issues", [])),
        vlm_recommendations=json.dumps(vlm.get("recommendations", [])),
        verdict=session.get("verdict", "rejected"),
        rejection_reason=session.get("rejection_reason"),
        image_path=image_path,
    )

    table.add([record.dict()])


def setup_exchanges(channel: pika.channel.Channel) -> None:
    """Declare all RabbitMQ exchanges required by this process.

    Idempotent — safe to call on every startup. Uses durable=True
    so exchanges survive broker restarts.

    Args:
        channel: An open pika channel.
    """
    channel.exchange_declare(
        exchange="scorer.results", exchange_type="topic", durable=True
    )
    channel.exchange_declare(
        exchange="loop.accepted", exchange_type="topic", durable=True
    )
    channel.exchange_declare(
        exchange="scorer.events", exchange_type="topic", durable=True
    )


def on_clip_result(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a CLIP scorer result message.

    Updates the in-flight session record in Redis with the CLIP score
    and image embedding. Does not trigger a LanceDB write — that occurs
    on cancel or accepted events.

    If the session key is not found in Redis, the session has expired
    or was already written. The message is acked and ignored.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties.
        body: JSON-encoded bytes containing image_uuid, clip_score,
            and image_embedding.
    """
    result = json.loads(body)
    image_uuid = result["image_uuid"]
    key = f"ldb:session:{image_uuid}"
    raw = r.get(key)
    if not raw:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    session = json.loads(raw)
    session["clip"] = result
    r.setex(key, SCORE_TIMEOUT, json.dumps(session))
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_artifact_result(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle an artifact scorer result message.

    Updates the in-flight session record in Redis with the artifact
    confidence score. Does not trigger a LanceDB write.

    If the session key is not found in Redis, the session has expired
    or was already written. The message is acked and ignored.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties.
        body: JSON-encoded bytes containing image_uuid and ai_confidence.
    """
    result = json.loads(body)
    image_uuid = result["image_uuid"]
    key = f"ldb:session:{image_uuid}"
    raw = r.get(key)
    if not raw:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    session = json.loads(raw)
    session["artifact"] = result
    r.setex(key, SCORE_TIMEOUT, json.dumps(session))
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_vlm_result(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a VLM scorer result message.

    Updates the in-flight session record in Redis with the VLM scores,
    issues, and recommendations. Does not trigger a LanceDB write.

    If the session key is not found in Redis, the session has expired
    or was already written. The message is acked and ignored.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties.
        body: JSON-encoded bytes containing image_uuid and VLM scores.
    """
    result = json.loads(body)
    image_uuid = result["image_uuid"]
    key = f"ldb:session:{image_uuid}"
    raw = r.get(key)
    if not raw:
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    session = json.loads(raw)
    session["vlm"] = result
    r.setex(key, SCORE_TIMEOUT, json.dumps(session))
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_cancel(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a cancel event from the aggregator.

    Atomically removes the session record from Redis via GETDEL and
    writes a rejected loop record to LanceDB with null image_path.

    The GETDEL operation is atomic — in a scale-out deployment with
    multiple lancedb_manager instances, only one instance will receive
    a non-null response and proceed with the write.

    If the session key is not found, the session has already been
    processed or has expired. The message is acked and ignored.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties.
        body: JSON-encoded bytes containing image_uuid and reason.
    """
    msg = json.loads(body)
    image_uuid = msg["image_uuid"]
    key = f"ldb:session:{image_uuid}"
    raw = r.getdel(key)
    if raw:
        session = json.loads(raw)
        session["verdict"] = "rejected"
        session["rejection_reason"] = msg.get("reason")
        write_loop_record(session, image_path=None)
    ch.basic_ack(delivery_tag=method.delivery_tag)


def on_accepted(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle an accepted event from the tactical LLM.

    Atomically removes the session record from Redis via GETDEL and
    writes a candidate loop record to LanceDB with the image_path
    provided in the message.

    The GETDEL operation is atomic — in a scale-out deployment with
    multiple lancedb_manager instances, only one instance will receive
    a non-null response and proceed with the write.

    If the session key is not found, the session has already been
    processed or has expired. The message is acked and ignored.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag.
        props: Message properties.
        body: JSON-encoded bytes containing image_uuid and image_path.
    """
    msg = json.loads(body)
    image_uuid = msg["image_uuid"]
    key = f"ldb:session:{image_uuid}"
    raw = r.getdel(key)
    if raw:
        session = json.loads(raw)
        session["verdict"] = "candidate"
        session["rejection_reason"] = None
        write_loop_record(session, image_path=msg.get("image_path"))
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    """Start the LanceDB manager process.

    Ensures LanceDB tables exist, connects to RabbitMQ, declares all
    required exchanges and queues, binds queues to exchanges, and
    begins consuming messages.

    This function blocks indefinitely. It is the process entry point.
    """
    ensure_tables()

    connection = pika.BlockingConnection(
        pika.URLParameters(_cfg.broker["rabbitmq_url"])
    )
    channel = connection.channel()
    setup_exchanges(channel)

    for queue, exchange, binding in [
        ("lancedb.clip.queue", "scorer.results", "clip.*"),
        ("lancedb.artifact.queue", "scorer.results", "artifact.*"),
        ("lancedb.vlm.queue", "scorer.results", "vlm.*"),
        ("lancedb.cancel.queue", "scorer.events", "cancel.*"),
        ("lancedb.accepted.queue", "loop.accepted", "accepted.*"),
    ]:
        channel.queue_declare(queue=queue, durable=True)
        channel.queue_bind(
            exchange=exchange,
            queue=queue,
            routing_key=binding,
        )

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(
        queue="lancedb.clip.queue", on_message_callback=on_clip_result
    )
    channel.basic_consume(
        queue="lancedb.artifact.queue", on_message_callback=on_artifact_result
    )
    channel.basic_consume(
        queue="lancedb.vlm.queue", on_message_callback=on_vlm_result
    )
    channel.basic_consume(
        queue="lancedb.cancel.queue", on_message_callback=on_cancel
    )
    channel.basic_consume(
        queue="lancedb.accepted.queue", on_message_callback=on_accepted
    )
    print("LanceDB manager ready")
    channel.start_consuming()


if __name__ == "__main__":
    main()
