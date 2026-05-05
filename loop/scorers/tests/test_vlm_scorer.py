import threading
from unittest.mock import patch

from tests.conftest import make_frame


def test_cancel_event_set_on_events_message(mock_conn):
    from vlm_scorer import _SUB_EVENTS, _Listener, active_jobs, jobs_lock

    image_uuid = "test-cancel-uuid"
    cancel_event = threading.Event()
    with jobs_lock:
        active_jobs[image_uuid] = cancel_event
    frame = make_frame(_SUB_EVENTS, "msg-1", {"image_uuid": image_uuid})
    _Listener(mock_conn).on_message(frame)
    assert cancel_event.is_set()
    mock_conn.ack.assert_called_once_with("msg-1", _SUB_EVENTS)


def test_score_worker_respects_cancel_event(mock_conn):
    with patch("vlm_scorer.llm") as mock_llm:
        mock_llm.return_value = iter([{"choices": [{"text": '{"photo'}]}])
        cancel_event = threading.Event()
        cancel_event.set()
        from vlm_scorer import score_worker

        score_worker("test-uuid", "/tmp/test.png", "test prompt", cancel_event, mock_conn)
        mock_conn.send.assert_not_called()


def test_score_worker_handles_malformed_json(mock_conn):
    with patch("vlm_scorer.llm") as mock_llm:
        mock_llm.return_value = iter([{"choices": [{"text": "not valid json"}]}])
        cancel_event = threading.Event()
        from vlm_scorer import score_worker

        score_worker("test-uuid", "/tmp/test.png", "test prompt", cancel_event, mock_conn)
        mock_conn.send.assert_not_called()
