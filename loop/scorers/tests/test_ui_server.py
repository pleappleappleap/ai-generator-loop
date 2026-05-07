"""Tests for loop/ui/server.py — FastAPI UI backend."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# ── Path setup ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_UI_DIR = _REPO_ROOT / "loop" / "ui"
for _p in (str(_REPO_ROOT), str(_UI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Patch config.load during server import ────────────────────────────────────
# server.py calls _load_config() at module level.  We patch only config.load so
# that server._cfg is a controlled stub, while the real config module (with all
# its helper functions like resolve_backend) stays in sys.modules intact for the
# scorer tests that also run in this pytest session.


class _FakeCfg:
    tactical = {"model": {"server_url": "http://localhost:8080/v1", "model_name": "default"}}
    database = {"path": "/tmp/test_pipeline.db"}
    broker = {"stomp_url": "stomp://localhost:61613"}

    def get(self, key, default=None):  # noqa: ANN001
        return default


# Ensure the real config module is in sys.modules before patching.
import config as _config_module  # noqa: E402, F401

with patch("config.load", return_value=_FakeCfg()):
    import server  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """TestClient with the STOMP startup stubbed out."""
    with patch("server._start_stomp"):
        with TestClient(server.app, raise_server_exceptions=True) as c:
            yield c


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_frame(destination: str, body: dict) -> MagicMock:
    """Build a mock stomp Frame with the given destination and JSON body."""
    frame = MagicMock()
    frame.headers = {"destination": destination}
    frame.body = json.dumps(body)
    return frame


def _close_coro(coro: object, loop: object) -> MagicMock:  # noqa: ARG001
    """Side-effect for run_coroutine_threadsafe that closes the coroutine to
    suppress 'coroutine was never awaited' warnings."""
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


# ── _PipelineListener ─────────────────────────────────────────────────────────


def test_listener_loop_events_caches_image_path():
    """loop.events message should write image_uuid → image_path into _image_paths."""
    loop = asyncio.new_event_loop()
    listener = server._PipelineListener(loop)
    server._image_paths.clear()

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_coro):
        listener.on_message(_make_frame("/topic/loop.events", {
            "image_uuid": "img-001",
            "image_path": "/tmp/out.png",
            "workflow_id": "wf-1",
            "conversation_id": "conv-1",
            "sequence_number": 1,
        }))

    assert server._image_paths.get("img-001") == "/tmp/out.png"
    loop.close()


def test_listener_loop_events_schedules_gallery_broadcast():
    """loop.events should schedule a _gallery.broadcast coroutine on the loop."""
    loop = asyncio.new_event_loop()
    listener = server._PipelineListener(loop)
    server._image_paths.clear()

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_coro) as mock_schedule:
        listener.on_message(_make_frame("/topic/loop.events", {
            "image_uuid": "img-002",
            "image_path": "/tmp/out2.png",
            "workflow_id": "wf-2",
            "conversation_id": "conv-2",
            "sequence_number": 0,
        }))

    assert mock_schedule.called
    assert mock_schedule.call_args[0][1] is loop
    loop.close()


def test_listener_tactical_decisions_schedules_gallery_broadcast():
    """tactical.decisions should schedule a _gallery.broadcast coroutine on the loop."""
    loop = asyncio.new_event_loop()
    listener = server._PipelineListener(loop)

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_coro) as mock_schedule:
        listener.on_message(_make_frame("/topic/tactical.decisions", {
            "image_uuid": "img-003",
            "decision": {"decision": "accept", "reasoning": "looks good", "confidence": 0.9},
            "verdict": {"verdict": "accept", "workflow_id": "wf-3", "conversation_id": "conv-3"},
        }))

    assert mock_schedule.called
    assert mock_schedule.call_args[0][1] is loop
    loop.close()


def test_listener_ignores_malformed_json():
    """on_message with invalid JSON body should not raise."""
    loop = asyncio.new_event_loop()
    listener = server._PipelineListener(loop)
    frame = MagicMock()
    frame.headers = {"destination": "/topic/loop.events"}
    frame.body = "not-json{"
    listener.on_message(frame)  # must not raise
    loop.close()


def test_listener_on_error_writes_to_stderr(capsys: pytest.CaptureFixture) -> None:
    loop = asyncio.new_event_loop()
    listener = server._PipelineListener(loop)
    frame = MagicMock()
    frame.body = "broker disconnected"
    listener.on_error(frame)
    loop.close()
    assert "broker disconnected" in capsys.readouterr().err


# ── REST: GET / ───────────────────────────────────────────────────────────────


def test_root_serves_index_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200


# ── REST: POST /session/start ─────────────────────────────────────────────────


def test_session_start_calls_conversation_create(client: TestClient) -> None:
    with patch("server._coordinator_call",
               return_value={"ok": True, "conversation_id": "conv-x"}) as mock_call:
        resp = client.post("/session/start", json={"name": "My Session"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_call.assert_called_once_with({"op": "ConversationCreate", "name": "My Session"})


# ── REST: POST /workflow/new ──────────────────────────────────────────────────


def test_workflow_new_creates_then_resumes(client: TestClient) -> None:
    """Should call WorkflowCreate then WorkflowResume and return merged result."""
    def _fake(req: dict) -> dict:
        if req["op"] == "WorkflowCreate":
            return {"ok": True, "workflow_id": "wf-x", "conversation_id": "conv-x",
                    "workflow_path": "sdxl.json"}
        if req["op"] == "WorkflowResume":
            return {"ok": True, "run_id": "run-x"}
        return {"ok": False}

    with patch("server._coordinator_call", side_effect=_fake):
        resp = client.post("/workflow/new", json={
            "conversation_id": "conv-x",
            "workflow_path": "sdxl.json",
        })

    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["workflow_id"] == "wf-x"
    assert body["run_id"] == "run-x"
    assert body["conversation_id"] == "conv-x"


def test_workflow_new_propagates_create_failure(client: TestClient) -> None:
    with patch("server._coordinator_call", return_value={"ok": False, "error": "bad"}):
        resp = client.post("/workflow/new", json={
            "conversation_id": "conv-x",
            "workflow_path": "sdxl.json",
        })
    assert resp.json()["ok"] is False


# ── REST: POST /workflow/{id}/resume ──────────────────────────────────────────


def test_workflow_resume_calls_workflow_resume(client: TestClient) -> None:
    with patch("server._coordinator_call",
               return_value={"ok": True, "run_id": "run-y"}) as mock_call:
        resp = client.post("/workflow/wf-abc/resume", json={})

    assert resp.status_code == 200
    assert resp.json()["run_id"] == "run-y"
    mock_call.assert_called_once_with({"op": "WorkflowResume", "workflow_id": "wf-abc"})


def test_workflow_resume_forwards_optional_budget_fields(client: TestClient) -> None:
    with patch("server._coordinator_call", return_value={"ok": True, "run_id": "run-z"}) as mock_call:
        client.post("/workflow/wf-abc/resume", json={
            "budget_policy": "carry",
            "max_retries": 5,
            "max_inpaints": 3,
        })

    args = mock_call.call_args[0][0]
    assert args["budget_policy"] == "carry"
    assert args["max_retries"] == 5
    assert args["max_inpaints"] == 3


# ── REST: POST /workflow/{id}/start-run ───────────────────────────────────────


def test_workflow_start_run_enqueues_to_stomp(client: TestClient) -> None:
    mock_stomp = MagicMock()
    mock_stomp.is_connected.return_value = True

    with patch("server._stomp_conn", mock_stomp):
        resp = client.post("/workflow/wf-abc/start-run", json={
            "prompt": "a cat",
            "run_id": "run-1",
            "conversation_id": "conv-1",
            "workflow_path": "sdxl.json",
        })

    body = resp.json()
    assert body["ok"] is True
    assert "image_uuid" in body
    mock_stomp.send.assert_called_once()
    sent = json.loads(mock_stomp.send.call_args.kwargs["body"])
    assert sent["prompt"] == "a cat"
    assert sent["workflow_id"] == "wf-abc"
    assert sent["conversation_id"] == "conv-1"
    assert sent["session_uuid"] == "run-1"


def test_workflow_start_run_no_stomp_returns_error(client: TestClient) -> None:
    with patch("server._stomp_conn", None):
        resp = client.post("/workflow/wf-abc/start-run", json={
            "prompt": "a cat",
            "run_id": "run-1",
            "conversation_id": "conv-1",
            "workflow_path": "sdxl.json",
        })
    assert resp.json()["ok"] is False
    assert "error" in resp.json()


# ── REST: POST /feedback ──────────────────────────────────────────────────────


def test_feedback_calls_feedback_add(client: TestClient) -> None:
    with patch("server._coordinator_call", return_value={"ok": True}) as mock_call:
        resp = client.post("/feedback", json={
            "image_uuid": "img-001",
            "workflow_id": "wf-1",
            "run_id": "run-1",
            "rating": "up",
            "comment": "looks great",
        })

    assert resp.status_code == 200
    args = mock_call.call_args[0][0]
    assert args["op"] == "FeedbackAdd"
    assert args["rating"] == "up"
    assert args["image_uuid"] == "img-001"


# ── REST: GET /history ────────────────────────────────────────────────────────


def test_history_calls_context_get(client: TestClient) -> None:
    with patch("server._coordinator_call",
               return_value={"ok": True, "chat": [], "feedback": []}) as mock_call:
        resp = client.get("/history?conversation_id=conv-1&workflow_id=wf-1&limit=10")

    assert resp.status_code == 200
    args = mock_call.call_args[0][0]
    assert args["op"] == "ContextGet"
    assert args["conversation_id"] == "conv-1"
    assert args["workflow_id"] == "wf-1"
    assert args["limit"] == 10


# ── REST: GET /image/{uuid} ───────────────────────────────────────────────────


def test_image_not_in_cache_returns_404(client: TestClient) -> None:
    server._image_paths.clear()
    resp = client.get("/image/no-such-uuid")
    assert resp.status_code == 404


def test_image_local_file_served(client: TestClient) -> None:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        tmp = f.name

    server._image_paths["local-uuid"] = tmp
    try:
        resp = client.get("/image/local-uuid")
        assert resp.status_code == 200
        assert resp.content[:4] == b"\x89PNG"
    finally:
        server._image_paths.pop("local-uuid", None)
        Path(tmp).unlink(missing_ok=True)


def test_image_local_file_missing_returns_404(client: TestClient) -> None:
    server._image_paths["missing-uuid"] = "/tmp/does_not_exist_xyz.png"
    try:
        resp = client.get("/image/missing-uuid")
        assert resp.status_code == 404
    finally:
        server._image_paths.pop("missing-uuid", None)


def test_image_http_url_proxied(client: TestClient) -> None:
    server._image_paths["http-uuid"] = "http://comfyui:8188/view?filename=x.png"
    try:
        mock_resp = MagicMock()
        mock_resp.content = b"PNG_DATA"
        mock_resp.headers = {"content-type": "image/png"}

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_http):
            resp = client.get("/image/http-uuid")

        assert resp.status_code == 200
        assert resp.content == b"PNG_DATA"
    finally:
        server._image_paths.pop("http-uuid", None)


# ── _GalleryManager ───────────────────────────────────────────────────────────


def test_gallery_broadcast_reaches_all_clients() -> None:
    async def _run() -> None:
        manager = server._GalleryManager()
        ws1, ws2 = AsyncMock(), AsyncMock()
        manager._clients = {ws1, ws2}
        await manager.broadcast({"type": "image_ready", "image_uuid": "x"})
        expected = json.dumps({"type": "image_ready", "image_uuid": "x"})
        ws1.send_text.assert_called_once_with(expected)
        ws2.send_text.assert_called_once_with(expected)

    asyncio.run(_run())


def test_gallery_broadcast_removes_dead_clients() -> None:
    """Clients that raise on send_text should be silently dropped."""
    async def _run() -> None:
        manager = server._GalleryManager()
        dead = AsyncMock()
        dead.send_text.side_effect = Exception("connection lost")
        live = AsyncMock()
        manager._clients = {dead, live}
        await manager.broadcast({"type": "test"})
        assert dead not in manager._clients
        assert live in manager._clients

    asyncio.run(_run())


def test_gallery_connect_adds_client() -> None:
    async def _run() -> None:
        manager = server._GalleryManager()
        ws = AsyncMock()
        await manager.connect(ws)
        ws.accept.assert_called_once()
        assert ws in manager._clients

    asyncio.run(_run())


def test_gallery_disconnect_removes_client() -> None:
    async def _run() -> None:
        manager = server._GalleryManager()
        ws = AsyncMock()
        manager._clients = {ws}
        await manager.disconnect(ws)
        assert ws not in manager._clients

    asyncio.run(_run())
