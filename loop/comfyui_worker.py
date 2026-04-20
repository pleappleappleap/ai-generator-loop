"""ComfyUI MQ worker for the AI image generation pipeline.

Consumes generation requests from the loop.request queue, submits
them to the ComfyUI HTTP API, waits for completion via WebSocket,
retrieves the output image URL, and publishes a completion event to
the loop.events topic exchange.

ComfyUI must be running at COMFYUI_URL before this process starts.
Start ComfyUI with: ~/ai-image/loop/ComfyUI/launch.sh

Queue subscriptions:
  loop.request [queue] → on_request

Exchange publications:
  loop.events [topic] routing key: loop.complete.<image_uuid>
"""

import json
import sys
import uuid
from pathlib import Path

import pika
import pika.channel
import pika.spec
import redis as redis_lib
import requests
import websocket

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

COMFYUI_URL: str = _cfg.broker["comfyui_url"]
"""Base URL of the ComfyUI HTTP API."""

COMFYUI_WS: str = _cfg.broker["comfyui_ws"]
"""WebSocket URL for ComfyUI completion events."""

_r = redis_lib.Redis.from_url(_cfg.broker["redis_url"], decode_responses=True)
"""Redis connection for writing in-flight session records."""


def setup_exchanges(channel: pika.channel.Channel) -> None:
    """Declare the loop.events topic exchange.

    Idempotent — safe to call on every startup.

    Args:
        channel: An open pika channel.
    """
    channel.exchange_declare(
        exchange="loop.events",
        exchange_type="topic",
        durable=True,
    )


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


def on_request(
    ch: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    props: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """Handle a generation request from the loop.request queue.

    Loads the specified workflow JSON, submits it to ComfyUI with
    optional prompt override, waits for completion, retrieves the
    output image URL, and publishes a completion event to loop.events.

    Args:
        ch: The pika channel the message arrived on.
        method: Delivery metadata including delivery tag for acking.
        props: Message properties (unused).
        body: JSON-encoded bytes. Expected keys:
            image_uuid (str): UUID for this generated image.
            session_uuid (str): UUID of the parent session.
            sequence_number (int): Position within session.
            workflow_path (str): Path to ComfyUI API-format workflow JSON.
            prompt (str, optional): Prompt override for positive nodes.
            workflow_params (dict, optional): Generation parameters for
                storage in LanceDB.
    """
    request = json.loads(body)
    with open(request["workflow_path"]) as f:
        workflow = json.load(f)
    prompt_id = submit_workflow(workflow, request.get("prompt"))
    wait_for_completion(prompt_id)
    output_path = get_output_path(prompt_id)
    result = {
        "image_uuid": request["image_uuid"],
        "session_uuid": request["session_uuid"],
        "sequence_number": request["sequence_number"],
        "prompt_id": prompt_id,
        "image_path": output_path,
        "prompt": request.get("prompt"),
        "workflow_path": request["workflow_path"],
        "workflow_params": request.get("workflow_params", {}),
    }
    ttl = _cfg.thresholds["score_timeout_secs"]
    session_record = {
        "image_uuid":      request["image_uuid"],
        "session_uuid":    request["session_uuid"],
        "sequence_number": request["sequence_number"],
        "workflow_path":   request["workflow_path"],
        "workflow_params": request.get("workflow_params", {}),
        "prompt":          request.get("prompt", ""),
        "image_path":      None,
    }
    _r.setex(
        f"agg:session:{request['image_uuid']}",
        ttl,
        json.dumps(session_record),
    )
    _r.setex(
        f"ldb:session:{request['image_uuid']}",
        ttl * 2,
        json.dumps(session_record),
    )
    ch.basic_publish(
        exchange="loop.events",
        routing_key=f"loop.complete.{request['image_uuid']}",
        body=json.dumps(result),
    )
    ch.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    """Start the ComfyUI MQ worker process.

    Connects to RabbitMQ, declares the loop.events exchange and
    loop.request queue, and begins consuming generation requests.
    Blocks indefinitely. This is the process entry point.
    """
    connection = pika.BlockingConnection(pika.URLParameters(_cfg.broker["rabbitmq_url"]))
    channel = connection.channel()
    setup_exchanges(channel)
    channel.queue_declare(queue="loop.request", durable=True)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue="loop.request", on_message_callback=on_request)
    print("ComfyUI worker ready")
    channel.start_consuming()


if __name__ == "__main__":
    main()
