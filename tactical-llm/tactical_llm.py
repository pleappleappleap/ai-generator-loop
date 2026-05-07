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
the coordinator's SQLite ``tactical_budget`` table, accessed via a Unix
domain socket at ``{database.path}.sock``.

The LLM model is loaded once at process startup and held resident in GPU
memory. The inference loop runs on the main thread; STOMP consumption
uses a single-threaded listener so decisions are serialised.

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
import socket
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import openai
import stomp

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

from prompts import SYSTEM_PROMPT, build_decision_prompt  # noqa: E402
from retrieval import embed_prompt, session_history, similar_past  # noqa: E402

_cfg = _load_config()
_tac          = _cfg.tactical
_model_cfg    = _tac.get("model", {})
_decision_cfg = _tac.get("decisions", {})
_tac_compute  = _cfg.compute["tactical_llm"]

_COORDINATOR_SOCK: str = _cfg.database["path"] + ".sock"

_u = urlparse(_cfg.broker["stomp_url"])
_STOMP_HOST: str = _u.hostname or "localhost"
_STOMP_PORT: int = _u.port or 61613
_STOMP_USER: str = _u.username or ""
_STOMP_PASS: str = _u.password or ""

# ── LLM client (OpenAI-compatible llama.cpp server) ──────────────────────────

_LLM_SERVER_URL: str = _model_cfg.get("server_url", "http://localhost:8080/v1")
_LLM_MODEL_NAME: str = _model_cfg.get("model_name", "default")

_client = openai.OpenAI(base_url=_LLM_SERVER_URL, api_key="none")

_TEMPERATURE: float       = _model_cfg.get("temperature",          0.1)
_MAX_TOKENS: int          = _model_cfg.get("max_tokens",            512)
_MAX_TOKENS_THINKING: int = _model_cfg.get("max_tokens_thinking", 4096)

_MAX_RETRIES:  int   = _decision_cfg.get("max_retries",  3)
_MAX_INPAINTS: int   = _decision_cfg.get("max_inpaints", 2)
_VLM_MEAN_MIN: float = _decision_cfg.get("accept_vlm_mean_min", 7.0)
_CLIP_MIN:     float = _decision_cfg.get("accept_clip_min",     0.30)


# ── Coordinator socket helpers ────────────────────────────────────────────────


def _coordinator_call(req: dict) -> dict:
    """Send a JSON request to the coordinator Unix socket and return the response.

    Each request/response is a newline-terminated JSON object.

    Args:
        req: Dict to send as JSON.

    Returns:
        Parsed response dict.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(_COORDINATOR_SOCK)
        sock.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n")[0])
    finally:
        sock.close()


def _get_budget(session_uuid: str) -> dict:
    """Return the budget record for a session from the coordinator.

    If no budget record exists (session started before tactical LLM was
    running), initialises one with configured defaults and returns them.

    Args:
        session_uuid: The session UUID.

    Returns:
        Dict with ``retries_used``, ``inpaints_used``, ``max_retries``,
        and ``max_inpaints``.
    """
    resp = _coordinator_call({"op": "BudgetGet", "session_uuid": session_uuid})
    if resp.get("ok") and "retries_used" in resp:
        return {
            "retries_used":  resp["retries_used"],
            "inpaints_used": resp["inpaints_used"],
            "max_retries":   resp["max_retries"],
            "max_inpaints":  resp["max_inpaints"],
        }
    # No budget record — init with configured defaults.
    _coordinator_call({
        "op":          "BudgetInit",
        "session_uuid": session_uuid,
        "max_retries":  _MAX_RETRIES,
        "max_inpaints": _MAX_INPAINTS,
    })
    return {
        "retries_used":  0,
        "inpaints_used": 0,
        "max_retries":   _MAX_RETRIES,
        "max_inpaints":  _MAX_INPAINTS,
    }


def _increment_budget(session_uuid: str, field: str) -> None:
    """Atomically increment a budget counter via the coordinator.

    Args:
        session_uuid: The session UUID.
        field: One of ``"retries_used"`` or ``"inpaints_used"``.
    """
    _coordinator_call({"op": "BudgetUpdate", "session_uuid": session_uuid, "field": field})


# ── LLM inference ─────────────────────────────────────────────────────────────


def _run_inference(decision_prompt: str) -> dict:
    """Run a single tactical inference and return the parsed JSON decision.

    Detects whether the prompt ends with ``/think`` (Qwen3 chain-of-thought
    enabled) and selects the appropriate token limit. Strips the
    ``<think>…</think>`` reasoning block before JSON parsing if present.
    Strips any accidental markdown fencing. Returns an empty dict on parse
    failure — the caller falls back to :func:`_heuristic_decision`.

    Args:
        decision_prompt: The fully-constructed decision prompt from
            :func:`~prompts.build_decision_prompt`. Must end with either
            ``/think`` or ``/no_think``.

    Returns:
        Parsed decision dict, or empty dict on JSON parse failure.
    """
    thinking = decision_prompt.rstrip().endswith("/think")
    max_tok  = _MAX_TOKENS_THINKING if thinking else _MAX_TOKENS

    result = _client.chat.completions.create(
        model=_LLM_MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": decision_prompt},
        ],
        temperature=_TEMPERATURE,
        max_tokens=max_tok,
    )
    raw = result.choices[0].message.content.strip()

    # Strip Qwen3 chain-of-thought block when thinking mode was enabled.
    if "<think>" in raw:
        end = raw.find("</think>")
        if end == -1:
            # Unclosed block — model exhausted its token budget during
            # reasoning without producing JSON. Nothing to parse.
            return {}
        raw = raw[end + len("</think>"):].strip()

    # Strip accidental markdown fences.
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


def _publish_accepted(
    conn: stomp.Connection,
    image_uuid: str,
    image_path: str | None,
    scores: dict,
) -> None:
    """Publish an accepted verdict to the loop.accepted topic address.

    The LanceDB manager listens for these events and writes the final
    Loop record with the image_path.

    Args:
        conn: Open STOMP connection.
        image_uuid: UUID of the accepted image.
        image_path: Filesystem path to the accepted image file, or None.
        scores: Full scorer results dict from the verdict message.
    """
    payload = {
        "image_uuid": image_uuid,
        "image_path": image_path,
        "scores":     scores,
    }
    conn.send(
        destination="/topic/loop.accepted",
        body=json.dumps(payload),
        headers={"persistent": "true"},
    )


def _publish_retry(
    conn: stomp.Connection,
    original_verdict: dict,
    decision: dict,
) -> str:
    """Publish a retry generation request to the loop.request queue.

    Constructs a new request inheriting session_uuid and workflow_path
    from the original message but with a new image_uuid, incremented
    sequence_number, and the LLM-revised prompt.

    Args:
        conn: Open STOMP connection.
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
        "workflow_id":      original_verdict.get("workflow_id", ""),
        "conversation_id":  original_verdict.get("conversation_id", ""),
        "workflow_params":  {
            **(scores.get("workflow_params") or {}),
            **(decision.get("retry_params") or {}),
        },
        "prompt":           decision.get("retry_prompt", ""),
    }
    conn.send(
        destination="/queue/loop.request",
        body=json.dumps(request),
        headers={"persistent": "true"},
    )
    return new_image_uuid


def _publish_inpaint(
    conn: stomp.Connection,
    original_verdict: dict,
    decision: dict,
) -> None:
    """Publish an inpaint request to the loop.inpaint.request queue.

    This is a stub implementation. The inpaint workflow (ControlNet
    inpaint pipeline, SAM-guided mask generation) is not yet implemented.
    The message is published with the full decision payload for future
    consumers.

    Args:
        conn: Open STOMP connection.
        original_verdict: The full verdict dict from scorer.result.
        decision: The parsed LLM decision dict.
    """
    scores = original_verdict.get("scores", {})
    payload = {
        "image_uuid":      original_verdict.get("image_uuid"),
        "session_uuid":    scores.get("session_uuid"),
        "image_path":      scores.get("image_path"),
        "inpaint_regions": decision.get("inpaint_regions", []),
        "inpaint_prompt":  decision.get("inpaint_prompt", ""),
        "original_scores": scores,
    }
    conn.send(
        destination="/queue/loop.inpaint.request",
        body=json.dumps(payload),
        headers={"persistent": "true"},
    )


def _publish_decision_log(
    conn: stomp.Connection,
    image_uuid: str,
    session_uuid: str,
    decision: dict,
    raw_verdict: dict,
) -> None:
    """Publish the full decision to the tactical.decisions audit topic.

    The strategic LLM will consume from this topic to analyse decision
    patterns across sessions and tune thresholds or system prompts.

    Args:
        conn: Open STOMP connection.
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
    conn.send(
        destination="/topic/tactical.decisions",
        body=json.dumps(payload),
        headers={"persistent": "true"},
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


def _handle_verdict(conn: stomp.Connection, body: str | bytes) -> None:
    """Handle a verdict from the scorer.result queue.

    Retrieves LanceDB context, constructs the decision prompt, runs LLM
    inference, and acts on the result. Publishes to the audit topic
    regardless of outcome. The heuristic fallback always produces a valid
    decision, so inference failures do not cause message loss.

    Args:
        conn: Open STOMP connection for publishing.
        body: JSON-encoded verdict string or bytes from scorer.result.
    """
    verdict          = json.loads(body)
    image_uuid       = verdict.get("image_uuid", "")
    scores           = verdict.get("scores", {})
    session_uuid     = scores.get("session_uuid", "")
    image_path       = scores.get("image_path")
    workflow_id      = verdict.get("workflow_id", "")
    conversation_id  = verdict.get("conversation_id", "")

    budget  = _get_budget(session_uuid)
    history = session_history(session_uuid)

    prompt_text = scores.get("prompt", "")
    try:
        embedding = embed_prompt(prompt_text) if prompt_text else []
        past      = similar_past(embedding, session_uuid) if embedding else []
    except Exception:
        past = []

    context: dict | None = None
    if workflow_id and conversation_id:
        try:
            resp = _coordinator_call({
                "op":             "ContextGet",
                "conversation_id": conversation_id,
                "workflow_id":     workflow_id,
                "limit":           20,
            })
            if resp.get("ok"):
                context = resp
        except Exception:
            pass

    try:
        decision_prompt = build_decision_prompt(verdict, history, past, budget, context)
        decision = _run_inference(decision_prompt)
    except Exception:
        decision = {}

    if not decision or "decision" not in decision:
        decision = _heuristic_decision(verdict, budget)

    action = decision.get("decision", "give_up")

    if action == "accept":
        _publish_accepted(conn, image_uuid, image_path, scores)
        print(f"[tactical] accept  {image_uuid}  confidence={decision.get('confidence'):.2f}")

    elif action == "retry":
        if budget["retries_used"] >= budget["max_retries"]:
            action = "give_up"
            decision["decision"] = "give_up"
            decision["reasoning"] += " (retry budget exhausted — overriding to give_up)"
            print(f"[tactical] give_up {image_uuid}  (retry budget exhausted)")
        else:
            new_uuid = _publish_retry(conn, verdict, decision)
            _increment_budget(session_uuid, "retries_used")
            print(
                f"[tactical] retry   {image_uuid} → {new_uuid}  "
                f"({budget['retries_used'] + 1}/{budget['max_retries']})"
            )

    elif action == "inpaint":
        if budget["inpaints_used"] >= budget["max_inpaints"]:
            if budget["retries_used"] < budget["max_retries"]:
                action = "retry"
                decision["decision"] = "retry"
                decision["reasoning"] += " (inpaint budget exhausted — downgrading to retry)"
                if not decision.get("retry_prompt"):
                    decision["retry_prompt"] = prompt_text
                new_uuid = _publish_retry(conn, verdict, decision)
                _increment_budget(session_uuid, "retries_used")
                print(f"[tactical] retry   {image_uuid} → {new_uuid}  (inpaint budget exhausted)")
            else:
                action = "give_up"
                decision["decision"] = "give_up"
                print(f"[tactical] give_up {image_uuid}  (all budgets exhausted)")
        else:
            _publish_inpaint(conn, verdict, decision)
            _increment_budget(session_uuid, "inpaints_used")
            print(
                f"[tactical] inpaint {image_uuid}  "
                f"({budget['inpaints_used'] + 1}/{budget['max_inpaints']})"
            )

    else:  # give_up
        print(f"[tactical] give_up {image_uuid}  reasoning: {decision.get('reasoning', '')}")

    _publish_decision_log(conn, image_uuid, session_uuid, decision, verdict)


class _Listener(stomp.ConnectionListener):
    """STOMP message listener for the tactical LLM."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[tactical_llm] STOMP error: {frame.body}", file=sys.stderr)

    def on_message(self, frame: stomp.utils.Frame) -> None:
        msg_id = frame.headers.get("message-id", "")
        sub_id = frame.headers.get("subscription", "")
        try:
            _handle_verdict(self._conn, frame.body)
        finally:
            self._conn.ack(msg_id, sub_id)


def main() -> None:
    """Start the tactical LLM process.

    Connects to Artemis via STOMP, subscribes to scorer.result, and
    blocks indefinitely. This is the process entry point.
    """
    conn = stomp.Connection(host_and_ports=[(_STOMP_HOST, _STOMP_PORT)])
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER, _STOMP_PASS, wait=True,
        headers={"client-id": "tactical-llm"},
    )
    conn.subscribe(
        destination="/queue/scorer.result",
        id="1",
        ack="client-individual",
    )
    print("Tactical LLM ready")
    print(f"  llm_server:      {_LLM_SERVER_URL}")
    print(f"  max_retries:     {_MAX_RETRIES}")
    print(f"  max_inpaints:    {_MAX_INPAINTS}")
    print(f"  accept_vlm_mean: {_VLM_MEAN_MIN}")
    print(f"  accept_clip_min: {_CLIP_MIN}")
    while conn.is_connected():
        time.sleep(1)


if __name__ == "__main__":
    main()
