"""Session orchestration entry point for the ai-image pipeline.

Creates a new generation session, initialises tactical LLM budget state
via the coordinator Unix socket, writes the session record to LanceDB,
and publishes the first generation request to the ``loop.request`` queue.

Usage::

    python session.py --prompt "..."
    python session.py --prompt "..." --workflow /path/to/workflow.json
    python session.py --prompt "..." --max-retries 5 --max-inpaints 3
    python session.py --prompt "..." --monitor

The ``--monitor`` flag blocks until the session is resolved (accept or
give_up verdict), polling the coordinator for the budget state.

This script does not need to run inside the scorers venv — it only uses
the root venv (stomp, lancedb, open_clip, torch) and the shared
config.py module.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import lancedb
import stomp
import torch

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent / "loop" / "scorers"))

import open_clip  # noqa: E402 — available in scorers venv, accessed via sys.path

_cfg  = _load_config()
_tac  = _cfg.tactical
_dec  = _tac.get("decisions", {})

_MAX_RETRIES:  int = _dec.get("max_retries",  3)
_MAX_INPAINTS: int = _dec.get("max_inpaints", 2)

_COORDINATOR_SOCK: str = _cfg.database["path"] + ".sock"

_u = urlparse(_cfg.broker["stomp_url"])
_STOMP_HOST: str = _u.hostname or "localhost"
_STOMP_PORT: int = _u.port or 61613
_STOMP_USER: str = _u.username or ""
_STOMP_PASS: str = _u.password or ""


# ── CLIP embedding ────────────────────────────────────────────────────────────


def _embed_prompt(prompt: str) -> list[float]:
    """Return the 512-dimensional normalised CLIP text embedding for a prompt.

    Args:
        prompt: Natural language prompt to embed.

    Returns:
        List of 512 floats.
    """
    clip_cfg = _cfg.models["clip"]
    model, _, _ = open_clip.create_model_and_transforms(
        clip_cfg["name"], pretrained=clip_cfg["pretrained"]
    )
    tokenizer = open_clip.get_tokenizer(clip_cfg["name"])

    backend = _cfg.compute["clip_scorer"]["backend"]
    device = (
        torch.device("mps")  if backend == "mps" else
        torch.device("cuda") if backend in ("cuda", "rocm") else
        torch.device("cpu")
    )
    model = model.to(device)

    tokens = tokenizer([prompt]).to(device)
    with torch.no_grad():
        features = model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze().tolist()


# ── LanceDB session record ────────────────────────────────────────────────────


def _create_session_record(
    session_uuid: str,
    prompt: str,
    prompt_embedding: list[float],
) -> None:
    """Write a Session record to LanceDB.

    Creates the sessions table if it does not already exist.

    Args:
        session_uuid: UUID string for this session.
        prompt: The user-supplied prompt.
        prompt_embedding: Normalised CLIP text embedding of the prompt.
    """
    from lancedb_schema import Session  # noqa: E402

    db = lancedb.connect(_cfg.paths["lancedb"])
    if "sessions" not in db.table_names():
        db.create_table("sessions", schema=Session.to_arrow_schema())

    table = db.open_table("sessions")
    record = Session(
        session_uuid=session_uuid,
        created_at=datetime.now(timezone.utc).isoformat(),
        user_prompt=prompt,
        prompt_embedding=prompt_embedding,
    )
    table.add([record.model_dump()])


# ── Coordinator budget ────────────────────────────────────────────────────────


def _coordinator_call(req: dict) -> dict:
    """Send a JSON request to the coordinator Unix socket and return the response."""
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


def _init_budget(session_uuid: str, max_retries: int, max_inpaints: int) -> None:
    """Initialise the tactical budget record for a session via the coordinator.

    Args:
        session_uuid: UUID string for this session.
        max_retries: Maximum number of retry generations allowed.
        max_inpaints: Maximum number of inpaint passes allowed.
    """
    _coordinator_call({
        "op":           "BudgetInit",
        "session_uuid": session_uuid,
        "max_retries":  max_retries,
        "max_inpaints": max_inpaints,
    })


# ── STOMP request publish ─────────────────────────────────────────────────────


def _publish_request(
    conn: stomp.Connection,
    session_uuid: str,
    image_uuid: str,
    workflow_path: str,
    prompt: str,
    workflow_params: dict,
) -> None:
    """Publish the first generation request to the loop.request queue.

    Args:
        conn: Open STOMP connection.
        session_uuid: UUID string for this session.
        image_uuid: UUID string for the first generated image.
        workflow_path: Absolute path to the ComfyUI workflow JSON.
        prompt: The user-supplied prompt.
        workflow_params: Optional dict of workflow parameter overrides.
    """
    request = {
        "image_uuid":      image_uuid,
        "session_uuid":    session_uuid,
        "sequence_number": 1,
        "workflow_path":   workflow_path,
        "prompt":          prompt,
        "workflow_params": workflow_params,
    }
    conn.send(
        destination="/queue/loop.request",
        body=json.dumps(request),
        headers={"persistent": "true"},
    )


# ── Monitor ───────────────────────────────────────────────────────────────────


def _monitor(session_uuid: str, poll_interval: float = 2.0) -> None:
    """Block until the session resolves, printing budget status each tick.

    A session is considered resolved when the tactical LLM has either
    accepted an image (budget still has headroom) or exhausted all budgets.
    Since there is no explicit "session complete" signal in the current
    architecture, resolution is detected by the absence of any pending
    images (i.e. the budget counter stops changing for 30 seconds).

    Args:
        session_uuid: UUID string of the session to monitor.
        poll_interval: Seconds between coordinator polls.
    """
    last_snapshot: dict | None = None
    stable  = 0.0
    timeout = 30.0

    print(f"\nMonitoring session {session_uuid} …  (Ctrl-C to detach)\n")

    try:
        while True:
            resp = _coordinator_call({"op": "BudgetGet", "session_uuid": session_uuid})
            if not resp.get("ok"):
                print(f"\nBudget record not found — session {session_uuid} likely completed.")
                break

            budget = {
                "retries_used":  resp["retries_used"],
                "inpaints_used": resp["inpaints_used"],
                "max_retries":   resp["max_retries"],
                "max_inpaints":  resp["max_inpaints"],
            }
            status = (
                f"  retries:  {budget['retries_used']}/{budget['max_retries']}  "
                f"inpaints: {budget['inpaints_used']}/{budget['max_inpaints']}"
            )
            print(f"\r{status}", end="", flush=True)

            exhausted = (
                budget["retries_used"]  >= budget["max_retries"] and
                budget["inpaints_used"] >= budget["max_inpaints"]
            )
            if exhausted:
                print(f"\nSession {session_uuid}: all budgets exhausted.")
                break

            if budget == last_snapshot:
                stable += poll_interval
            else:
                stable = 0.0
                last_snapshot = budget

            if stable >= timeout:
                print(f"\nNo budget changes for {timeout}s — assuming session resolved.")
                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\nDetached from monitor.")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Parse CLI arguments, create session, and publish first request."""
    parser = argparse.ArgumentParser(
        description="Start a new ai-image generation session."
    )
    parser.add_argument(
        "--prompt", required=True,
        help="Natural language prompt for image generation.",
    )
    parser.add_argument(
        "--workflow",
        default=str(Path(_cfg.paths["comfyui"]) / "workflows" / "default.json"),
        help="Path to ComfyUI API-format workflow JSON.",
    )
    parser.add_argument(
        "--max-retries", type=int, default=_MAX_RETRIES,
        help=f"Maximum retry count for this session (default: {_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--max-inpaints", type=int, default=_MAX_INPAINTS,
        help=f"Maximum inpaint passes for this session (default: {_MAX_INPAINTS}).",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="Block after publishing and monitor budget state.",
    )
    parser.add_argument(
        "--workflow-params", type=json.loads, default={},
        metavar="JSON",
        help='JSON dict of workflow parameter overrides, e.g. \'{"cfg": 7.5}\'.',
    )
    args = parser.parse_args()

    session_uuid = str(uuid.uuid4())
    image_uuid   = str(uuid.uuid4())

    print(f"Session UUID:  {session_uuid}")
    print(f"Image UUID:    {image_uuid}")
    print(f"Prompt:        {args.prompt[:80]}")
    print(f"Workflow:      {args.workflow}")
    print(f"Max retries:   {args.max_retries}")
    print(f"Max inpaints:  {args.max_inpaints}")

    # Embed prompt for LanceDB session record
    print("Embedding prompt …")
    try:
        embedding = _embed_prompt(args.prompt)
    except Exception as exc:
        print(f"Warning: could not embed prompt ({exc}); storing zero vector.")
        embedding = [0.0] * 512

    # Write session record to LanceDB
    try:
        _create_session_record(session_uuid, args.prompt, embedding)
        print("Session record written to LanceDB.")
    except Exception as exc:
        print(f"Warning: could not write session record ({exc}). Continuing.")

    # Initialise budget via coordinator
    _init_budget(session_uuid, args.max_retries, args.max_inpaints)
    print("Budget initialised in coordinator.")

    # Publish first generation request
    conn = stomp.Connection(host_and_ports=[(_STOMP_HOST, _STOMP_PORT)])
    conn.connect(_STOMP_USER, _STOMP_PASS, wait=True)
    _publish_request(conn, session_uuid, image_uuid, args.workflow, args.prompt, args.workflow_params)
    conn.disconnect()
    print("Generation request published → loop.request")

    if args.monitor:
        _monitor(session_uuid)


if __name__ == "__main__":
    main()
