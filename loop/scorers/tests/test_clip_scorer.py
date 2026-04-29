import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from tests.conftest import mock_channel, mock_method, mock_props

_FAKE_IMAGE = str(Path(tempfile.gettempdir()) / "test.png")


def test_score_returns_clip_score_and_embedding():
    with (
        patch("clip_scorer.model") as mock_model,
        patch("clip_scorer.preprocess") as mock_preprocess,
        patch("clip_scorer.tokenizer") as mock_tokenizer,
        patch("clip_scorer.Image"),
    ):
        import torch
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


def test_on_cancel_sets_cancel_event(mock_channel, mock_method, mock_props):
    from clip_scorer import active_jobs, jobs_lock, on_cancel
    image_uuid = "test-cancel-uuid"
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    body = json.dumps({"image_uuid": image_uuid}).encode()
    on_cancel(mock_channel, mock_method, mock_props, body)
    assert cancel_event.is_set()
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)


def test_on_cancel_unknown_uuid_does_not_raise(mock_channel, mock_method, mock_props):
    from clip_scorer import on_cancel
    body = json.dumps({"image_uuid": "unknown-uuid"}).encode()
    on_cancel(mock_channel, mock_method, mock_props, body)
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)
