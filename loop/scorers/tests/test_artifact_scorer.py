import json
import threading
from unittest.mock import patch
import pytest
from tests.conftest import mock_channel, mock_method, mock_props


def test_score_returns_ai_confidence():
    with patch("artifact_scorer.detector") as mock_detector:
        mock_detector.return_value = [
            {"label": "artificial", "score": 0.85},
            {"label": "real", "score": 0.15},
        ]
        from artifact_scorer import score
        result = score("/tmp/test.png")
        assert "ai_confidence" in result
        assert result["ai_confidence"] == pytest.approx(0.85)


def test_score_missing_artificial_label_returns_zero():
    with patch("artifact_scorer.detector") as mock_detector:
        mock_detector.return_value = [{"label": "real", "score": 1.0}]
        from artifact_scorer import score
        result = score("/tmp/test.png")
        assert result["ai_confidence"] == 0.0


def test_on_cancel_sets_cancel_event(mock_channel, mock_method, mock_props):
    from artifact_scorer import active_jobs, jobs_lock, on_cancel
    image_uuid = "test-cancel-uuid"
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    body = json.dumps({"image_uuid": image_uuid}).encode()
    on_cancel(mock_channel, mock_method, mock_props, body)
    assert cancel_event.is_set()
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)
