import json
from unittest.mock import patch
import pytest
from tests.conftest import fake_redis, mock_channel, mock_method, mock_props

IMAGE_UUID = "test-image-uuid"
SESSION_KEY = f"ldb:session:{IMAGE_UUID}"
SCORE_TIMEOUT = 120
BASE_SESSION = {
    "image_uuid": IMAGE_UUID, "session_uuid": "test-session-uuid",
    "sequence_number": 1, "prompt": "test prompt",
    "workflow_path": "/tmp/workflow.json", "workflow_params": {},
    "clip": {"image_uuid": IMAGE_UUID, "clip_score": 0.8, "image_embedding": [0.0] * 512},
    "artifact": {"image_uuid": IMAGE_UUID, "ai_confidence": 0.2},
    "vlm": {"image_uuid": IMAGE_UUID, "photorealism": 8, "anatomical_coherence": 7,
            "interaction_plausibility": 8, "lighting_consistency": 7, "prompt_adherence": 9,
            "issues": [], "recommendations": []},
}


def test_on_clip_result_updates_session(fake_redis, mock_channel, mock_method, mock_props):
    fake_redis.setex(SESSION_KEY, SCORE_TIMEOUT, json.dumps(
        {"image_uuid": IMAGE_UUID, "clip": None, "artifact": None, "vlm": None}
    ))
    body = json.dumps({"image_uuid": IMAGE_UUID, "clip_score": 0.8, "image_embedding": [0.0] * 512}).encode()
    with patch("lancedb_manager.r", fake_redis):
        from lancedb_manager import on_clip_result
        on_clip_result(mock_channel, mock_method, mock_props, body)
    raw = fake_redis.get(SESSION_KEY)
    assert raw is not None
    assert json.loads(raw)["clip"]["clip_score"] == pytest.approx(0.8)


def test_on_cancel_writes_rejected_record(fake_redis, mock_channel, mock_method, mock_props):
    fake_redis.setex(SESSION_KEY, SCORE_TIMEOUT, json.dumps(BASE_SESSION))
    body = json.dumps({"image_uuid": IMAGE_UUID, "reason": "clip_threshold"}).encode()
    with (
        patch("lancedb_manager.r", fake_redis),
        patch("lancedb_manager.write_loop_record") as mock_write,
        patch("lancedb_manager.encode_prompt", return_value=[0.0] * 512),
    ):
        from lancedb_manager import on_cancel
        on_cancel(mock_channel, mock_method, mock_props, body)
    assert fake_redis.get(SESSION_KEY) is None
    mock_write.assert_called_once()
    assert mock_write.call_args.kwargs.get("image_path") is None


def test_on_accepted_writes_candidate_record(fake_redis, mock_channel, mock_method, mock_props):
    fake_redis.setex(SESSION_KEY, SCORE_TIMEOUT, json.dumps(BASE_SESSION))
    body = json.dumps({"image_uuid": IMAGE_UUID, "image_path": "/tmp/output/test.png"}).encode()
    with (
        patch("lancedb_manager.r", fake_redis),
        patch("lancedb_manager.write_loop_record") as mock_write,
        patch("lancedb_manager.encode_prompt", return_value=[0.0] * 512),
    ):
        from lancedb_manager import on_accepted
        on_accepted(mock_channel, mock_method, mock_props, body)
    assert fake_redis.get(SESSION_KEY) is None
    mock_write.assert_called_once()
    assert mock_write.call_args.kwargs.get("image_path") == "/tmp/output/test.png"
