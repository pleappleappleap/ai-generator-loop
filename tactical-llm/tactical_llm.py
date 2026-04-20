"""Tactical LLM decision engine — main process.

Consumes verdicts from the ``scorer.result`` queue. For each verdict,
retrieves context from LanceDB, runs local LLM inference, and acts on
the decision:

  accept   → publishes to the ``loop.accepted`` topic exchange
  retry    → re-publishes a modified generation request to ``loop.request``
  inpaint  → publishes to the ``loop.inpaint.request`` queue (stub)
  give_up  → logs and discards; no further action

Every decision is published to the ``tactical.decisions`` topic exchange
for auditing and eventual strategic LLM consumption.

Budget state (retries_used, inpaints_used) is tracked per session in
Redis under ``tactical:budget:{session_uuid}``. The session entry point
(:mod:`tactical-llm.session`) initialises this key when a session starts.

The LLM model is loaded once at process startup and held resident in GPU
memory. The inference loop runs on the main thread; RabbitMQ consumption
uses a single threaded channel (prefetch=1) so decisions are serialised.

Queue / exchange subscriptions:

  scorer.result  [queue]     → on_verdict

Exchange publications:

  loop.accepted     [topic]  routing key: accepted.<image_uuid>
  loop.inpaint.request [queue] (stub — inpaint not yet implemented)
  tactical.decisions [topic] routing key: decision.<verdict>.<image_uuid>

Queue publications:

  loop.request  [queue]  retry requests (same schema as session entry point)
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pika
import pika.channel
import pika.spec
import redis as redis_lib
from llama_cpp import Llama

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

from prompts import SYSTEM_PROMPT, build_decision_prompt  # noqa: E402
from retrieval import embed_prompt, session_history, similar_past  # noqa: E402

_cfg = _load_config()
_tac = _cfg.tactical
_model_cfg = _tac.get("model", {})
_decision_cfg = _tac.get("decisions", {})

_r = redis_lib.Redis.from_url(_cfg.broker["redis_url"], decode_responses=True)

# ── LLM initialisation ────────────────────────────────────────────────────────

_model_path = str(
    Path(_model_cfg.get("dir", "")) / _model_cfg.get("filename", "")
)

llm = Llama(
    model_path=_model_path,
    n_ctx=_model_cfg.get("context_length", 8192),
    n_gpu_layers=_model_cfg.get("n_gpu_layers", -1),
    verbose=False,
)

_TEMPERATURE: float = _model_cfg.get("temperature", 0.1)
_MAX_TOKENS: int    = _model_cfg.get("max_tokens", 512)

_MAX_RETRIES:  int   = _decision_cfg.get("max_retries",  3)
_MAX_INPAINTS: int   = _decision_cfg.get("max_inpaints", 2)
_VLM_MEAN_MIN: float = _decision_cfg.get("accept_vlm_mean_min", 7.0)
_CLIP_MIN:     float = _decision_cfg.get("accept_clip_min",     0.30)

_BUDGET_TTL: int = 86400  # 24h; sessions shouldn't run longer than this


# ── Redis budget helpers ──────────────────────────────────────────────────────


def _get_budget(session_uuid: str) -> dict:
    """Return the budget record for a session, creating a default if absent.

    Args:
        session_uuid: The session UUID.

    Returns:
        Dict with ``retries_used``, ``inpaints_used``, ``max_retries``,
        and ``max_inpaints``.
    """
    key = f"tactical:budget:{session_uuid}"
    raw = _r.get(key)
    if raw:
        return json.loads(raw)
    # No budget record — session started before tactical LLM was running.
    # Use configured defaults.
    budget = {
        "retries_used":  0,
        "inpaints_used": 0,
        "max_retries":   _MAX_RETRIES,
        "max_inpaints":  _MAX_INPAINTS,
    }
    _r.setex(key, _BUDGET_TTL, json.dumps(budget))
    return budget


def _save_budget(session_uuid: str, budget: dict) -> None:
    """Persist an updated budget record to Redis.

    Args:
        session_uuid: The session UUID.
        budget: Updated budget dict.
    """
    key = f"tactical:budget:{session_uuid}"
    _r.setex(key, _BUDGET_TTL, json.dumps(budget))


def _increment_budget(session_uuid: str, field: str) -> None:
    """Increment a budget counter in Redis.

    Args:
        session_uuid: The session UUID.
        field: One of ``"retries_used"`` or ``"inpaints_used"``.
    """
    budget = _get_budget(session_uuid)
    budget[field] = budget.get(field, 0) + 1
    _save_budget(session_uuid, budget)


# ── LLM inference ─────────────────────────────────────────────────────────────


def _run_inference(decision_prompt: str) -> dict:
    """Run a single tactical inference and return the parsed JSON decision.

    Calls the loaded ``llm`` with the system prompt and decision prompt.
    Strips any accidental markdown fencing before JSON parsing. Returns an
    empty dict on parse failure — the caller handles the fallback.

    Args:
        decision_prompt: The fully-constructed decision prompt from
            :func:`~prompts.build_decision_prompt`.

    Returns:
        Parsed decision dict, or empty dict on JSON parse failure.
    """
    result = llm.create_chat_completion(
        messages=[
            {"role": "system",  "content": SYSTEM_PROMPT},
            {"role": "user",    "content": decision_prompt},
        ],
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
    )
    raw = result["choices"][0]["message"]["content"].strip()
    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ── Decision execution ────────────────────────────────────────────────────────


def _setup_exchanges(channel: pika.channel.Channel) -> None:
    """Declare all exchanges and queues used by the tactical LLM.

    Idempotent — safe to call on every startup.

    Args:
        channel: An open pika channel.
    """
    channel.exchange_declare(
        exchange="loop.accepted",
        exchange_type="topic",
        durable=True,
    )
    channel.exchange_declare(
        exchange="tactical.decisions",
        exchange_type="topic",
        durable=True,
    )
    channel.queue_declare(queue="loop.request",           durable=True)
    channel.queue_declare(queue="loop.inpaint.request",   durable=True)
    channel.queue_declare(queue="scorer.result",          durable=True)


def _publish_accepted(
    channel: pika.channel.Channel,
    image_uuid: str,
    image_path: str | None,
    scores: dict,
) -> None:
    """Publish an accepted verdict to the loop.accepted topic exchange.

    The LanceDB manager listens for these events and writes the final
    Loop record with the image_path.

    Args:
        channel: Open pika channel.
        image_uuid: UUID of the accepted image.
        image_path: Filesystem path to the accepted image file, or None.
        scores: Full scorer results dict from the verdict message.
    """
    payload = {
        "image_uuid": image_uuid,
        "image_path": image_path,
        "scores":     scores,
    }
    channel.basic_publish(
        exchange="loop.accepted",
        routing_key=f"accepted.{image_uuid}",
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2),
    )


def _publish_retry(
    channel: pika.channel.Channel,
    original_verdict: dict,
    decision: dict,
) -> str:
    """Publish a retry generation request to the loop.request queue.

    Constructs a new request inheriting session_uuid and workflow_path
    from the original message but with a new image_uuid, incremented
    sequence_number, and the LLM-revised prompt.

    Args:
        channel: Open pika channel.
        original_verdict: The full verdict dict from scorer.result.
        decision: The parsed LLM decision dict.

    Returns:
        The new image_uuid string for the retry request.
    """
    scores = original_verdict.get("scores", {})
    new_image_uuid = str(uuid.uuid4())
    seq = scores.get("sequence_number", 0)
    request = {
        "image_uuid":       new_image_uuid,
        "session_uuid":     scores.get("session_uuid", ""),
        "sequence_number":  seq + 1,
        "workflow_path":    scores.get("workflow_path", ""),
        "workflow_params":  {
            **(scores.get("workflow_params") or {}),
            **(decision.get("retry_params") or {}),
        },
        "prompt":           decision.get("retry_prompt", ""),
    }
    channel.basic_publish(
        exchange="",
        routing_key="loop.request",
        body=json.dumps(request),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    return new_image_uuid


def _publish_inpaint(
    channel: pika.channel.Channel,
    original_verdict: dict,
    decision: dict,
) -> None:
    """Publish an inpaint request to the loop.inpaint.request queue.

    This is a stub implementation. The inpaint workflow (ControlNet
    inpaint pipeline, SAM-guided mask generation) is not yet implemented.
    The message is published with the full decision payload for future
    consumers.

    Args:
        channel: Open pika channel.
        original_verdict: The full verdict dict from scorer.result.
        decision: The parsed LLM decision dict.
    """
    scores = original_verdict.get("scores", {})
    payload = {
        "image_uuid":     original_verdict.get("image_uuid"),
        "session_uuid":   scores.get("session_uuid"),
        "image_path":     scores.get("image_path"),
        "inpaint_regions": decision.get("inpaint_regions", []),
        "inpaint_prompt": decision.get("inpaint_prompt", ""),
        "original_scores": scores,
    }
    channel.basic_publish(
        exchange="",
        routing_key="loop.inpaint.request",
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2),
    )


def _publish_decision_log(
    channel: pika.channel.Channel,
    image_uuid: str,
    session_uuid: str,
    decision: dict,
    raw_verdict: dict,
) -> None:
    """Publish the full decision to the tactical.decisions audit exchange.

    The strategic LLM will consume from this exchange to analyse decision
    patterns across sessions and tune thresholds or system prompts.

    Args:
        channel: Open pika channel.
        image_uuid: UUID of the image this decision was made for.
        session_uuid: Session UUID.
        decision: Parsed LLM decision dict.
        raw_verdict: Original verdict message from scorer.result.
    """
    payload = {
        "image_uuid":   image_uuid,
        "session_uuid": session_uuid,
        "decision":     decision,
        "verdict":      raw_verdict,
    }
    decision_type = decision.get("decision", "unknown")
    channel.basic_publish(
        exchange="tactical.decisions",
        routing_key=f"decision.{decision_type}.{image_uuid}",
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2),
    )


# ── Fallback heuristics ───────────────────────────────────────────────────────


def _heuristic_decision(verdict: dict, budget: dict) -> dict:
    """Return a heuristic decision when LLM inference produces invalid JSON.

    Applies simple threshold rules as a fallback so the pipeline continues
    running even if the LLM produces malformed output.

    Args:
        verdict: Full verdict dict from scorer.result.
        budget: Budget dict for this session.

    Returns:
        Minimal decision dict with ``"decision"`` and ``"reasoning"`` keys.
    """
    scores  = verdict.get("scores", {})
    clip    = scores.get("clip",     {}).get("clip_score",     0.0)
    vlm     = scores.get("vlm",      {})
    v_type  = verdict.get("verdict", "rejected")
    reason  = verdict.get("reason")

    if v_type == "rejected":
        if budget["retries_used"] < budget["max_retries"]:
            return {
                "decision":    "retry",
                "reasoning":   f"Image rejected ({reason}); heuristic retry.",
                "confidence":  0.5,
                "retry_prompt": scores.get("prompt", ""),
            }
        return {"decision": "give_up", "reasoning": "Budget exhausted.", "confidence": 1.0}

    dims = [
        vlm.get("photorealism"),
        vlm.get("anatomical_coherence"),
        vlm.get("interaction_plausibility"),
        vlm.get("lighting_consistency"),
        vlm.get("prompt_adherence"),
    ]
    valid = [d for d in dims if d is not None]
    mean  = sum(valid) / len(valid) if valid else 0.0

    if mean >= _VLM_MEAN_MIN and clip >= _CLIP_MIN:
        return {"decision": "accept", "reasoning": "Heuristic accept.", "confidence": 0.7}
    if budget["retries_used"] < budget["max_retries"]:
        return {
            "decision":    "retry",
            "reasoning":   f"Heuristic retry (vlm_mean={mean:.1f}, clip={clip:.3f}).",
            "confidence":  0.5,
            "retry_prompt": scores.get("prompt", ""),
        }
    return {"decision": "give_up", "reasoning": "Budget exhausted (heuristic).", "confidence": 1.0}


# ── Main consumer ─────────────────────────────────────────────────────────────


def on_verdict(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a verdict from the scorer.result queue.

    Retrieves LanceDB context, constructs the decision prompt, runs LLM
    inference, and acts on the result.  Publishes to the audit exchange
    regardless of outcome.  Acks the message on completion; does not nack
    on inference failure — the heuristic fallback always produces a valid
    decision.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag for acking.
        props: Message properties (unused).
        body: JSON-encoded verdict bytes from the scorer.result queue.
    """
    verdict     = json.loads(body)
    image_uuid  = verdict.get("image_uuid", "")
    scores      = verdict.get("scores", {})
    session_uuid = scores.get("session_uuid", "")
    image_path  = scores.get("image_path")

    budget  = _get_budget(session_uuid)
    history = session_history(session_uuid)

    # Build prompt embedding for retrieval
    prompt_text = scores.get("prompt", "")
    try:
        embedding = embed_prompt(prompt_text) if prompt_text else []
        past      = similar_past(embedding, session_uuid) if embedding else []
    except Exception:
        past = []

    # Run LLM inference
    try:
        decision_prompt = build_decision_prompt(verdict, history, past, budget)
        decision = _run_inference(decision_prompt)
    except Exception:
        decision = {}

    # Fall back to heuristics if LLM produces no parseable output
    if not decision or "decision" not in decision:
        decision = _heuristic_decision(verdict, budget)

    action = decision.get("decision", "give_up")

    # Execute decision
    if action == "accept":
        _publish_accepted(ch, image_uuid, image_path, scores)
        print(f"[tactical] accept  {image_uuid}  confidence={decision.get('confidence'):.2f}")

    elif action == "retry":
        if budget["retries_used"] >= budget["max_retries"]:
            action = "give_up"
            decision["decision"] = "give_up"
            decision["reasoning"] += " (retry budget exhausted — overriding to give_up)"
            print(f"[tactical] give_up {image_uuid}  (retry budget exhausted)")
        else:
            new_uuid = _publish_retry(ch, verdict, decision)
            _increment_budget(session_uuid, "retries_used")
            print(
                f"[tactical] retry   {image_uuid} → {new_uuid}  "
                f"({budget['retries_used'] + 1}/{budget['max_retries']})"
            )

    elif action == "inpaint":
        if budget["inpaints_used"] >= budget["max_inpaints"]:
            # Downgrade to retry if inpaint budget gone but retry available
            if budget["retries_used"] < budget["max_retries"]:
                action = "retry"
                decision["decision"] = "retry"
                decision["reasoning"] += " (inpaint budget exhausted — downgrading to retry)"
                if not decision.get("retry_prompt"):
                    decision["retry_prompt"] = prompt_text
                new_uuid = _publish_retry(ch, verdict, decision)
                _increment_budget(session_uuid, "retries_used")
                print(f"[tactical] retry   {image_uuid} → {new_uuid}  (inpaint budget exhausted)")
            else:
                action = "give_up"
                decision["decision"] = "give_up"
                print(f"[tactical] give_up {image_uuid}  (all budgets exhausted)")
        else:
            _publish_inpaint(ch, verdict, decision)
            _increment_budget(session_uuid, "inpaints_used")
            print(
                f"[tactical] inpaint {image_uuid}  "
                f"({budget['inpaints_used'] + 1}/{budget['max_inpaints']})"
            )

    else:  # give_up
        print(f"[tactical] give_up {image_uuid}  reasoning: {decision.get('reasoning', '')}")

    _publish_decision_log(ch, image_uuid, session_uuid, decision, verdict)
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    """Start the tactical LLM process.

    Connects to RabbitMQ, declares exchanges and queues, and begins
    consuming verdicts from scorer.result.  Blocks indefinitely.
    This is the process entry point.
    """
    connection = pika.BlockingConnection(
        pika.URLParameters(_cfg.broker["rabbitmq_url"])
    )
    channel = connection.channel()
    _setup_exchanges(channel)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue="scorer.result", on_message_callback=on_verdict)
    print("Tactical LLM ready")
    print(f"  model:           {_model_path}")
    print(f"  max_retries:     {_MAX_RETRIES}")
    print(f"  max_inpaints:    {_MAX_INPAINTS}")
    print(f"  accept_vlm_mean: {_VLM_MEAN_MIN}")
    print(f"  accept_clip_min: {_CLIP_MIN}")
    channel.start_consuming()


if __name__ == "__main__":
    main()
