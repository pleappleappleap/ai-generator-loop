import json
import threading
from unittest.mock import patch
from tests.conftest import mock_channel, mock_method, mock_props


def test_on_cancel_sets_cancel_event(mock_channel, mock_method, mock_props):
    from vlm_scorer import active_jobs, jobs_lock, on_cancel
    image_uuid = "test-cancel-uuid"
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    body = json.dumps({"image_uuid": image_uuid}).encode()
    on_cancel(mock_channel, mock_method, mock_props, body)
    assert cancel_event.is_set()
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)


def test_score_worker_respects_cancel_event(mock_channel):
    with patch("vlm_scorer.llm") as mock_llm:
        def slow_tokens(*args, **kwargs):
            yield {"choices": [{"text": '{"photo'}]}
        mock_llm.return_value = slow_tokens()
        cancel_event = threading.Event()
        cancel_event.set()
        from vlm_scorer import score_worker
        score_worker("test-uuid", "/tmp/test.png", "test prompt", cancel_event, mock_channel)
        mock_channel.basic_publish.assert_not_called()


def test_score_worker_handles_malformed_json(mock_channel):
    with patch("vlm_scorer.llm") as mock_llm:
        mock_llm.return_value = iter([{"choices": [{"text": "not valid json"}]}])
        cancel_event = threading.Event()
        from vlm_scorer import score_worker
        score_worker("test-uuid", "/tmp/test.png", "test prompt", cancel_event, mock_channel)
        mock_channel.basic_publish.assert_not_called()
