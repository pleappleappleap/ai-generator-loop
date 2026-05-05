import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import torch

from tests.conftest import make_frame

_FAKE_IMAGE = str(Path(tempfile.gettempdir()) / "test.png")


def test_score_returns_clip_score_and_embedding():
    with (
        patch("clip_scorer.model") as mock_model,
        patch("clip_scorer.preprocess") as mock_preprocess,
        patch("clip_scorer.tokenizer") as mock_tokenizer,
        patch("clip_scorer.Image"),
    ):
        mock_model.encode_image.return_value = torch.ones(1, 512)
        mock_model.encode_text.return_value = torch.ones(1, 512)
        mock_preprocess.return_value.unsqueeze.return_value = torch.ones(1, 3, 224, 224)
        mock_tokenizer.return_value = torch.ones(1, 77, dtype=torch.long)
        from clip_scorer import score

        result = score(_FAKE_IMAGE, "test prompt")
        assert "clip_score" in result
        assert "image_embedding" in result
        assert isinstance(result["clip_score"], float)
        assert isinstance(result["image_embedding"], list)
        assert len(result["image_embedding"]) == 512


def test_cancel_event_set_on_events_message(mock_conn):
    from clip_scorer import _SUB_EVENTS, _Listener, active_jobs, jobs_lock

    image_uuid = "test-cancel-uuid"
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    frame = make_frame(_SUB_EVENTS, "msg-1", {"image_uuid": image_uuid})
    _Listener(mock_conn).on_message(frame)
    assert cancel_event.is_set()
    mock_conn.ack.assert_called_once_with("msg-1", _SUB_EVENTS)


def test_cancel_unknown_uuid_does_not_raise(mock_conn):
    from clip_scorer import _SUB_EVENTS, _Listener

    frame = make_frame(_SUB_EVENTS, "msg-2", {"image_uuid": "unknown-uuid"})
    _Listener(mock_conn).on_message(frame)
    mock_conn.ack.assert_called_once_with("msg-2", _SUB_EVENTS)
