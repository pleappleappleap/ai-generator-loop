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

import copy

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


def detect_model_type(workflow: dict) -> str:
    """Infer model type from workflow node classes.

    Returns ``"flux"`` if the workflow contains Flux-specific loader nodes,
    ``"sdxl"`` if it contains a CheckpointLoaderSimple, or ``"unknown"``
    if neither is present.
    """
    class_types = {node.get("class_type", "") for node in workflow.values()}
    if "UNETLoader" in class_types or "FluxGuidance" in class_types:
        return "flux"
    if "CheckpointLoaderSimple" in class_types:
        return "sdxl"
    return "unknown"


def _find_node_by_class(workflow: dict, class_type: str) -> str | None:
    """Return the node ID of the first node matching *class_type*, or None."""
    for node_id, node in workflow.items():
        if node.get("class_type") == class_type:
            return node_id
    return None


def patch_checkpoint(workflow: dict, model_type: str) -> dict:
    """Substitute checkpoint / UNet / VAE / CLIP filenames from config.

    Reads ``models.sdxl.checkpoint`` or ``models.flux.*`` from config and
    updates the matching loader nodes in the workflow.  Nodes whose
    corresponding config value is empty are left unchanged.

    Args:
        workflow: ComfyUI API-format workflow dict.
        model_type: ``"sdxl"`` or ``"flux"``.

    Returns:
        A deep copy of the workflow with loader inputs patched.
    """
    workflow = copy.deepcopy(workflow)
    models = _cfg.models

    if model_type == "sdxl":
        checkpoint = (models.get("sdxl") or {}).get("checkpoint", "")
        if checkpoint:
            for node in workflow.values():
                if node.get("class_type") == "CheckpointLoaderSimple":
                    node["inputs"]["ckpt_name"] = checkpoint

    elif model_type == "flux":
        flux = models.get("flux") or {}
        for node in workflow.values():
            ct = node.get("class_type", "")
            if ct == "UNETLoader" and flux.get("unet"):
                node["inputs"]["unet_name"] = flux["unet"]
            elif ct == "VAELoader" and flux.get("vae"):
                node["inputs"]["vae_name"] = flux["vae"]
            elif ct == "DualCLIPLoader":
                if flux.get("clip_l"):
                    node["inputs"]["clip_name1"] = flux["clip_l"]
                if flux.get("t5xxl"):
                    node["inputs"]["clip_name2"] = flux["t5xxl"]

    return workflow


def inject_loras(workflow: dict, loras: list[dict], model_type: str) -> dict:
    """Insert LoraLoader nodes into the workflow graph.

    Finds the model and CLIP source nodes for the given *model_type*, then
    chains the requested LoRAs between those nodes and all downstream
    consumers.  Existing nodes that consumed model/CLIP outputs are updated
    to consume the final LoRA's outputs instead.

    Args:
        workflow: ComfyUI API-format workflow dict.
        loras: List of dicts with keys:
            ``filename``        — LoRA file as it appears in ComfyUI's loras dir
            ``strength_model``  — float weight applied to the UNet (default 1.0)
            ``strength_clip``   — float weight applied to CLIP (default 1.0)
            ``name``            — human label used in node title (optional)
        model_type: ``"sdxl"`` or ``"flux"``.

    Returns:
        A deep copy of the workflow with LoRA nodes inserted and upstream
        references updated.
    """
    if not loras:
        return workflow

    workflow = copy.deepcopy(workflow)

    # Locate the source nodes that provide model and clip outputs.
    if model_type == "sdxl":
        model_node_id = _find_node_by_class(workflow, "CheckpointLoaderSimple")
        if not model_node_id:
            return workflow
        clip_node_id = model_node_id
        model_output_idx = 0
        clip_output_idx = 1
    elif model_type == "flux":
        model_node_id = _find_node_by_class(workflow, "UNETLoader")
        clip_node_id = _find_node_by_class(workflow, "DualCLIPLoader")
        if not model_node_id or not clip_node_id:
            return workflow
        model_output_idx = 0
        clip_output_idx = 0
    else:
        return workflow

    model_source = (model_node_id, model_output_idx)
    clip_source = (clip_node_id, clip_output_idx)

    # Compute a starting node ID beyond all existing IDs.
    max_id = max(
        (int(k) for k in workflow if k.isdigit()),
        default=0,
    )

    new_lora_ids: set[str] = set()
    current_model = list(model_source)
    current_clip = list(clip_source)

    for lora in loras:
        max_id += 1
        lora_id = str(max_id)
        new_lora_ids.add(lora_id)
        strength = lora.get("default_strength", 1.0)
        workflow[lora_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model":          list(current_model),
                "clip":           list(current_clip),
                "lora_name":      lora["filename"],
                "strength_model": lora.get("strength_model", strength),
                "strength_clip":  lora.get("strength_clip",  strength),
            },
            "_meta": {"title": f"LoRA: {lora.get('name', lora['filename'])}"},
        }
        current_model = [lora_id, 0]
        current_clip  = [lora_id, 1]

    # Redirect all pre-existing nodes that consumed the original source outputs.
    for node_id, node in workflow.items():
        if node_id in new_lora_ids:
            continue  # don't touch the LoRA nodes we just added
        for key, val in node.get("inputs", {}).items():
            if isinstance(val, list) and len(val) == 2:
                ref = (val[0], val[1])
                if ref == model_source:
                    node["inputs"][key] = list(current_model)
                elif ref == clip_source:
                    node["inputs"][key] = list(current_clip)

    return workflow


def _resolve_loras(requested: list[dict], model_type: str) -> list[dict]:
    """Map LLM-chosen LoRA names to their config entries.

    The LLM decision contains ``{"name": "...", "strength_model": 0.8, ...}``.
    This function looks up the corresponding ``filename`` from config so the
    workflow patcher has what it needs.

    Unrecognised names are skipped with a warning.
    """
    registry = {
        entry["name"]: entry
        for entry in (_cfg.models.get("loras") or {}).get(model_type, [])
        if "name" in entry and "filename" in entry
    }
    resolved = []
    for req in requested:
        name = req.get("name", "")
        entry = registry.get(name)
        if not entry:
            print(f"[comfyui_worker] WARNING: unknown LoRA '{name}' for {model_type} — skipping")
            continue
        resolved.append({
            **entry,
            "strength_model": req.get("strength_model", entry.get("default_strength", 1.0)),
            "strength_clip":  req.get("strength_clip",  entry.get("default_strength", 1.0)),
        })
    return resolved


def patch_workflow(
    workflow: dict,
    model_type: str | None,
    loras: list[dict] | None,
) -> dict:
    """Apply all runtime patches to a ComfyUI workflow.

    Detects model type when *model_type* is ``None`` or ``"auto"``, patches
    checkpoint / loader node filenames from config, then injects any
    requested LoRA nodes.

    Args:
        workflow: ComfyUI API-format workflow dict.
        model_type: ``"sdxl"``, ``"flux"``, ``"auto"``/``None`` (auto-detect).
        loras: List of LoRA dicts from the LLM decision (name + strengths).

    Returns:
        Patched workflow dict (deep copy; original is not modified).
    """
    effective_type = model_type or detect_model_type(workflow)
    workflow = patch_checkpoint(workflow, effective_type)
    resolved = _resolve_loras(loras or [], effective_type)
    workflow = inject_loras(workflow, resolved, effective_type)
    return workflow


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
            workflow = patch_workflow(
                workflow,
                request.get("model_type"),
                request.get("loras"),
            )
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
