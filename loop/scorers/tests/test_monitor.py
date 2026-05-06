"""Tests for loop/monitor.py — pipeline dead-letter consumer."""

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# monitor.py lives in loop/, one level above loop/scorers/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.conftest import make_frame  # noqa: E402


# ---------------------------------------------------------------------------
# _format_dead_letter
# ---------------------------------------------------------------------------


def test_format_dead_letter_valid_json():
    from monitor import _format_dead_letter

    headers = {
        "_AMQ_ORIG_ADDRESS": "aggregator.clip.queue",
        "message-id": "ID:abc-123",
    }
    body = '{"image_uuid": "u1", "clip_score": 0.42}'
    out = _format_dead_letter(headers, body)

    assert "DEAD LETTER" in out
    assert "aggregator.clip.queue" in out
    assert "ID:abc-123" in out
    assert '"clip_score": 0.42' in out


def test_format_dead_letter_invalid_json():
    from monitor import _format_dead_letter

    headers = {"destination": "/queue/pipeline.dead", "message-id": "ID:bad"}
    body = "not-json"
    out = _format_dead_letter(headers, body)

    assert "DEAD LETTER" in out
    assert "not-json" in out  # repr'd in output


def test_format_dead_letter_bytes_body():
    from monitor import _format_dead_letter

    headers = {"message-id": "ID:bytes"}
    body = b'{"foo": 1}'
    out = _format_dead_letter(headers, body)

    assert '"foo": 1' in out


def test_format_dead_letter_falls_back_to_destination():
    from monitor import _format_dead_letter

    # No _AMQ_ORIG_ADDRESS — should fall back to destination header
    headers = {"destination": "/queue/pipeline.dead", "message-id": "ID:x"}
    out = _format_dead_letter(headers, "{}")
    assert "/queue/pipeline.dead" in out


# ---------------------------------------------------------------------------
# _Listener
# ---------------------------------------------------------------------------


def test_listener_on_message_acks_and_counts(mock_conn, capsys):
    from monitor import _Listener

    listener = _Listener(mock_conn)
    frame = make_frame("1", "ID:msg-1", {"image_uuid": "u1"})

    assert listener.count == 0
    listener.on_message(frame)

    mock_conn.ack.assert_called_once_with("ID:msg-1", "1")
    assert listener.count == 1
    captured = capsys.readouterr()
    assert "DEAD LETTER" in captured.out


def test_listener_on_message_acks_even_on_malformed_body(mock_conn):
    from monitor import _Listener

    listener = _Listener(mock_conn)
    frame = MagicMock()
    frame.headers = {"subscription": "1", "message-id": "ID:bad"}
    frame.body = b"\xff\xfe"  # invalid UTF-8 / JSON

    listener.on_message(frame)
    mock_conn.ack.assert_called_once_with("ID:bad", "1")


def test_listener_on_error_writes_stderr(mock_conn, capsys):
    from monitor import _Listener

    listener = _Listener(mock_conn)
    err_frame = MagicMock()
    err_frame.body = "broker disconnected"
    listener.on_error(err_frame)

    captured = capsys.readouterr()
    assert "broker disconnected" in captured.err


def test_listener_count_accumulates(mock_conn):
    from monitor import _Listener

    listener = _Listener(mock_conn)
    for i in range(5):
        frame = make_frame("1", f"ID:{i}", {"n": i})
        listener.on_message(frame)

    assert listener.count == 5
