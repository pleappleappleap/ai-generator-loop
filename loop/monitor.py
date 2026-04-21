"""Pipeline dead-letter monitor.

Consumes messages from the ``pipeline.dead`` queue (the dead letter
destination for all pipeline queues) and logs them for operator review.
Messages arrive here when a consumer nacks with requeue=false — i.e.,
after an unrecoverable processing failure.

This process is read-only: it acks every dead-letter message after
logging it. It never republishes. Human intervention is required to
investigate and replay failed messages.

Address subscriptions (STOMP / Artemis):
  /queue/pipeline.dead  → dead-letter messages from all pipeline queues

The ``pipeline.dead`` queue must be declared in the Artemis broker.xml
(or auto-created) and each pipeline queue must be configured with a
dead-letter address of ``pipeline.dlx`` routing to ``pipeline.dead``.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import stomp

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

_u = urlparse(_cfg.broker["stomp_url"])
_STOMP_HOST: str = _u.hostname or "localhost"
_STOMP_PORT: int = _u.port or 61613
_STOMP_USER: str = _u.username or ""
_STOMP_PASS: str = _u.password or ""


def _format_dead_letter(headers: dict, body: str | bytes) -> str:
    """Format a dead-letter message for human-readable log output.

    Args:
        headers: STOMP frame headers from the dead-letter message.
        body: Raw message body (JSON string or bytes).

    Returns:
        Multi-line string suitable for stdout logging.
    """
    ts = datetime.now(timezone.utc).isoformat()
    origin  = headers.get("_AMQ_ORIG_ADDRESS", headers.get("destination", "unknown"))
    msg_id  = headers.get("message-id", "unknown")

    try:
        payload = json.loads(body)
        body_pretty = json.dumps(payload, indent=2)
    except (json.JSONDecodeError, TypeError):
        body_pretty = repr(body)

    return (
        f"\n{'='*60}\n"
        f"DEAD LETTER  {ts}\n"
        f"  origin:     {origin}\n"
        f"  message-id: {msg_id}\n"
        f"  body:\n"
        + "\n".join(f"    {line}" for line in body_pretty.splitlines())
        + f"\n{'='*60}"
    )


class _Listener(stomp.ConnectionListener):
    """STOMP listener for the pipeline.dead dead-letter queue."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn
        self._count = 0

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[monitor] STOMP error: {frame.body}", file=sys.stderr)

    def on_message(self, frame: stomp.utils.Frame) -> None:
        msg_id = frame.headers.get("message-id", "")
        sub_id = frame.headers.get("subscription", "")
        try:
            print(_format_dead_letter(frame.headers, frame.body), flush=True)
            self._count += 1
        finally:
            self._conn.ack(msg_id, sub_id)

    @property
    def count(self) -> int:
        return self._count


def main() -> None:
    """Start the dead-letter monitor process.

    Connects to Artemis via STOMP, subscribes to pipeline.dead, and
    logs all arriving dead-letter messages. Blocks indefinitely.
    This is the process entry point.
    """
    conn = stomp.Connection(host_and_ports=[(_STOMP_HOST, _STOMP_PORT)])
    listener = _Listener(conn)
    conn.set_listener("", listener)
    conn.connect(
        _STOMP_USER, _STOMP_PASS, wait=True,
        headers={"client-id": "pipeline-monitor"},
    )
    conn.subscribe(
        destination="/queue/pipeline.dead",
        id="1",
        ack="client-individual",
    )
    print("Pipeline monitor ready — watching pipeline.dead", flush=True)
    while conn.is_connected():
        time.sleep(1)


if __name__ == "__main__":
    main()
