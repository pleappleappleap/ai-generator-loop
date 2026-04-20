import json
from unittest.mock import patch
import pytest
from tests.conftest import fake_redis, mock_channel, mock_method, mock_props

SCORE_TIMEOUT = 60
IMAGE_UUID = "test-image-uuid"
SESSION_KEY = f"agg:session:{IMAGE_UUID}"
PASSING_SESSION = {"image_uuid": IMAGE_UUID, "clip": None, "artifact": None, "vlm": None}
CLIP_PASS = {"image_uuid": IMAGE_UUID, "clip_score": 0.8, "image_embedding": [0.0] * 512}
CLIP_FAIL = {"image_uuid": IMAGE_UUID, "clip_score": 0.1, "image_embedding": [0.0] * 512}
ARTIFACT_PASS = {"image_uuid": IMAGE_UUID, "ai_confidence": 0.2}
ARTIFACT_FAIL = {"image_uuid": IMAGE_UUID, "ai_confidence": 0.9}
VLM_PASS = {"image_uuid": IMAGE_UUID, "photorealism": 8, "anatomical_coherence": 7,
            "interaction_plausibility": 8, "lighting_consistency": 7, "prompt_adherence": 9,
            "issues": [], "recommendations": []}


def setup_session(fake_redis, session=None):
    if session is None:
        session = PASSING_SESSION.copy()
    fake_redis.setex(SESSION_KEY, SCORE_TIMEOUT, json.dumps(session))


def test_clip_failure_triggers_cancel_and_rejected(fake_redis, mock_channel, mock_method, mock_props):
    setup_session(fake_redis)
    body = json.dumps(CLIP_FAIL).encode()
    with patch("scorer_aggregator.r", fake_redis):
        from scorer_aggregator import on_clip_result
        on_clip_result(mock_channel, mock_method, mock_props, body)
    assert fake_redis.get(SESSION_KEY) is None
    assert mock_channel.basic_publish.call_count == 2


def test_clip_pass_updates_session(fake_redis, mock_channel, mock_method, mock_props):
    setup_session(fake_redis)
    body = json.dumps(CLIP_PASS).encode()
    with patch("scorer_aggregator.r", fake_redis):
        from scorer_aggregator import on_clip_result
        on_clip_result(mock_channel, mock_method, mock_props, body)
    raw = fake_redis.get(SESSION_KEY)
    assert raw is not None
    assert json.loads(raw)["clip"]["clip_score"] == pytest.approx(0.8)
    mock_channel.basic_publish.assert_not_called()


def test_all_scores_pass_emits_candidate(fake_redis, mock_channel, mock_method, mock_props):
    session = PASSING_SESSION.copy()
    session["clip"] = CLIP_PASS
    session["artifact"] = ARTIFACT_PASS
    setup_session(fake_redis, session)
    body = json.dumps(VLM_PASS).encode()
    with patch("scorer_aggregator.r", fake_redis):
        from scorer_aggregator import on_vlm_result
        on_vlm_result(mock_channel, mock_method, mock_props, body)
    assert fake_redis.get(SESSION_KEY) is None
    mock_channel.basic_publish.assert_called_once()


def test_expired_session_is_ignored(fake_redis, mock_channel, mock_method, mock_props):
    body = json.dumps(CLIP_PASS).encode()
    with patch("scorer_aggregator.r", fake_redis):
        from scorer_aggregator import on_clip_result
        on_clip_result(mock_channel, mock_method, mock_props, body)
    mock_channel.basic_publish.assert_not_called()
