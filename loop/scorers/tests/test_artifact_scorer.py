import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import make_frame

_FAKE_IMAGE = str(Path(tempfile.gettempdir()) / "test.png")


def test_score_returns_ai_confidence():
    with patch("artifact_scorer.detector") as mock_detector:
        mock_detector.return_value = [
            {"label": "artificial", "score": 0.85},
            {"label": "real", "score": 0.15},
        ]
        from artifact_scorer import score

        result = score(_FAKE_IMAGE)
        assert "ai_confidence" in result
        assert result["ai_confidence"] == pytest.approx(0.85)


def test_score_missing_artificial_label_returns_zero():
    with patch("artifact_scorer.detector") as mock_detector:
        mock_detector.return_value = [{"label": "real", "score": 1.0}]
        from artifact_scorer import score

        result = score(_FAKE_IMAGE)
        assert result["ai_confidence"] == 0.0


def test_cancel_event_set_on_events_message(mock_conn):
    from artifact_scorer import _SUB_EVENTS, _Listener, active_jobs, jobs_lock

    image_uuid = "test-cancel-uuid"
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    frame = make_frame(_SUB_EVENTS, "msg-1", {"image_uuid": image_uuid})
    _Listener(mock_conn).on_message(frame)
    assert cancel_event.is_set()
    mock_conn.ack.assert_called_once_with("msg-1", _SUB_EVENTS)
