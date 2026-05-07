"""ComfyUI MQ worker for the AI image generation pipeline.

Consumes generation requests from the loop.request queue, submits
them to the ComfyUI HTTP API, waits for completion via WebSocket,
retrieves the output image URL, and publishes a completion event to
the loop.events topic address.

ComfyUI must be running at COMFYUI_URL before this process starts.
Start ComfyUI with: ~/ai-image/loop/ComfyUI/launch.sh

Address subscriptions (STOMP / Artemis):
  /queue/loop.request  → generation requests

Address publications (STOMP / Artemis):
  /topic/loop.events   → loop.complete.<image_uuid> events
"""

import json
import socket
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
import stomp
import websocket

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

COMFYUI_URL: str = _cfg.broker["comfyui_url"]
"""Base URL of the ComfyUI HTTP API."""

COMFYUI_WS: str = _cfg.broker["comfyui_ws"]
"""WebSocket URL for ComfyUI completion events."""

_COORDINATOR_SOCK: str = _cfg.database["path"] + ".sock"
"""Unix socket path for the coordinator API."""

_u = urlparse(_cfg.broker["stomp_url"])
_STOMP_HOST: str = _u.hostname or "localhost"
_STOMP_PORT: int = _u.port or 61613
_STOMP_USER: str = _u.username or ""
_STOMP_PASS: str = _u.password or ""

_disconnected = threading.Event()


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


def submit_workflow(
    workflow: dict,
    prompt_override: str | None = None,
) -> str:
    """Submit a workflow to the ComfyUI API and return the prompt ID.

    Optionally overrides the positive prompt text in any CLIPTextEncode
    node whose title contains the word "positive". This allows the
    tactical LLM to modify the prompt without reconstructing the entire
    workflow JSON.

    Args:
        workflow: ComfyUI API-format workflow dict, as loaded from a
            workflow JSON file exported via Save (API Format).
        prompt_override: If provided, replaces the text input of all
            positive CLIPTextEncode nodes in the workflow. If None,
            the workflow is submitted unchanged.

    Returns:
        The ComfyUI prompt ID string for the submitted job. Used to
        poll job status and retrieve output images.

    Raises:
        requests.HTTPError: If the ComfyUI API returns an error response.
        KeyError: If the API response does not contain a prompt_id field.
    """
    if prompt_override:
        for node in workflow.values():
            if node.get("class_type") == "CLIPTextEncode":
                if "positive" in node.get("_meta", {}).get("title", "").lower():
                    node["inputs"]["text"] = prompt_override
    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    response = requests.post(f"{COMFYUI_URL}/prompt", json=payload)
    return response.json()["prompt_id"]


def wait_for_completion(prompt_id: str) -> dict:
    """Block until a ComfyUI job completes and return its output.

    Connects to the ComfyUI WebSocket and listens for execution events.
    Returns when an "executed" event is received for the given prompt ID.

    Args:
        prompt_id: The ComfyUI prompt ID returned by submit_workflow.

    Returns:
        The output dict from the executed event, containing node outputs
        including image filenames and subfolders.
    """
    ws = websocket.WebSocket()
    ws.connect(COMFYUI_WS)
    while True:
        msg = json.loads(ws.recv())
        if (
            msg.get("type") == "executed"
            and msg.get("data", {}).get("prompt_id") == prompt_id
        ):
            ws.close()
            return msg["data"]["output"]


def get_output_path(prompt_id: str) -> str:
    """Retrieve the output image URL for a completed ComfyUI job.

    Queries the ComfyUI history endpoint and constructs a /view URL
    for the first image output found in the job's node outputs.

    Args:
        prompt_id: The ComfyUI prompt ID of a completed job.

    Returns:
        A URL string of the form:
        http://127.0.0.1:8188/view?filename=<f>&subfolder=<s>&type=<t>

    Raises:
        ValueError: If no image output is found in the job history.
    """
    history = requests.get(f"{COMFYUI_URL}/history/{prompt_id}").json()
    outputs = history[prompt_id]["outputs"]
    for node_output in outputs.values():
        if "images" in node_output:
            img = node_output["images"][0]
            return (
                f"{COMFYUI_URL}/view"
                f"?filename={img['filename']}"
                f"&subfolder={img['subfolder']}"
                f"&type={img['type']}"
            )
    raise ValueError(f"No image output found for prompt {prompt_id}")


class _Listener(stomp.ConnectionListener):
    """STOMP message listener for the ComfyUI worker."""

    def __init__(self, conn: stomp.Connection) -> None:
        self._conn = conn

    def on_error(self, frame: stomp.utils.Frame) -> None:
        print(f"[comfyui_worker] STOMP error: {frame.body}", file=sys.stderr)

    def on_disconnected(self) -> None:
        print("[comfyui_worker] STOMP disconnected — exiting", file=sys.stderr)
        _disconnected.set()

    def on_message(self, frame: stomp.utils.Frame) -> None:
        """Handle a generation request from the loop.request queue.

        Loads the specified workflow JSON, submits it to ComfyUI with
        optional prompt override, waits for completion, retrieves the
        output image URL, registers the image with the coordinator, and
        publishes a completion event to /topic/loop.events.

        Acks the message only after successful publish.
        """
        msg_id = frame.headers.get("message-id", "")
        sub_id = frame.headers.get("subscription", "")
        try:
            request = json.loads(frame.body)
            with open(request["workflow_path"]) as f:
                workflow = json.load(f)
            prompt_id = submit_workflow(workflow, request.get("prompt"))
            wait_for_completion(prompt_id)
            output_path = get_output_path(prompt_id)
            result = {
                "image_uuid":      request["image_uuid"],
                "session_uuid":    request["session_uuid"],
                "sequence_number": request["sequence_number"],
                "prompt_id":       prompt_id,
                "image_path":      output_path,
                "prompt":          request.get("prompt"),
                "workflow_path":   request["workflow_path"],
                "workflow_params": request.get("workflow_params", {}),
                "workflow_id":     request.get("workflow_id", ""),
                "conversation_id": request.get("conversation_id", ""),
            }
            score_timeout_secs = int(_cfg.thresholds["score_timeout_secs"])
            _coordinator_call({
                "op":                "SessionInit",
                "image_uuid":        request["image_uuid"],
                "session_uuid":      request["session_uuid"],
                "sequence_number":   request["sequence_number"],
                "prompt":            request.get("prompt", ""),
                "workflow_path":     request["workflow_path"],
                "workflow_params":   json.dumps(request.get("workflow_params", {})),
                "score_timeout_secs": score_timeout_secs,
                "workflow_id":       request.get("workflow_id", ""),
                "conversation_id":   request.get("conversation_id", ""),
            })
            self._conn.send(
                destination="/topic/loop.events",
                body=json.dumps(result),
                headers={"persistent": "true"},
            )
        finally:
            self._conn.ack(msg_id, sub_id)


def main() -> None:
    """Start the ComfyUI MQ worker process.

    Connects to Artemis via STOMP, subscribes to loop.request, and
    blocks indefinitely. This is the process entry point.
    """
    conn = stomp.Connection(
        host_and_ports=[(_STOMP_HOST, _STOMP_PORT)],
        heartbeats=(10000, 10000),
    )
    conn.set_listener("", _Listener(conn))
    conn.connect(
        _STOMP_USER, _STOMP_PASS, wait=True,
        headers={"client-id": "comfyui-worker"},
    )
    conn.subscribe(
        destination="/queue/loop.request",
        id="1",
        ack="client-individual",
    )
    print("ComfyUI worker ready")
    _disconnected.wait()
    sys.exit(1)


if __name__ == "__main__":
    main()
