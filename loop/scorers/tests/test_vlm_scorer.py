"""Tests for vlm_scorer.py.

Endpoint tests patch vlm_scorer._call_vlm and are fully platform-agnostic.
Backend-specific tests (llama.cpp / mlx-vlm internals) are skipped on the
wrong platform.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import vlm_scorer as _vlm_mod  # imported after conftest stubs backends

_VALID_JSON = json.dumps({
    "photorealism": 8,
    "anatomical_coherence": 7,
    "interaction_plausibility": 6,
    "lighting_consistency": 9,
    "prompt_adherence": 8,
    "issues": ["minor blur"],
    "recommendations": ["increase sharpness"],
})


# ── /health ───────────────────────────────────────────────────────────────────

def test_health(vlm_client):
    resp = vlm_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── /score ────────────────────────────────────────────────────────────────────

def test_score_returns_expected_fields(vlm_client):
    with patch("vlm_scorer._call_vlm", return_value=_VALID_JSON):
        resp = vlm_client.post("/score", json={
            "image_uuid": "uuid-1",
            "image_path": "/tmp/fake.png",
            "prompt": "a landscape",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["image_uuid"] == "uuid-1"
    for field in ("photorealism", "anatomical_coherence", "interaction_plausibility",
                  "lighting_consistency", "prompt_adherence"):
        assert field in data
        assert 0.0 <= data[field] <= 10.0
    assert "issues" in data
    assert "recommendations" in data


def test_score_clamps_out_of_range_fields(vlm_client):
    raw = json.dumps({
        "photorealism": 15,
        "anatomical_coherence": -3,
        "interaction_plausibility": 5,
        "lighting_consistency": 5,
        "prompt_adherence": 5,
        "issues": [],
        "recommendations": [],
    })
    with patch("vlm_scorer._call_vlm", return_value=raw):
        resp = vlm_client.post("/score", json={
            "image_uuid": "uuid-2",
            "image_path": "/tmp/fake.png",
            "prompt": "test",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["photorealism"] == 10.0
    assert data["anatomical_coherence"] == 0.0


def test_score_malformed_json_returns_422(vlm_client):
    with patch("vlm_scorer._call_vlm", return_value="not valid json }"):
        resp = vlm_client.post("/score", json={
            "image_uuid": "uuid-3",
            "image_path": "/tmp/fake.png",
            "prompt": "test",
        })

    assert resp.status_code == 422


def test_score_unreadable_image_returns_422(vlm_client):
    with patch("vlm_scorer._call_vlm", side_effect=OSError("no such file")):
        resp = vlm_client.post("/score", json={
            "image_uuid": "uuid-4",
            "image_path": "/nonexistent/img.png",
            "prompt": "test",
        })

    assert resp.status_code == 422


# ── /analyze ──────────────────────────────────────────────────────────────────

def test_analyze_returns_answer(vlm_client):
    with patch("vlm_scorer._call_vlm", return_value="The image shows a tiger."):
        resp = vlm_client.post("/analyze", json={
            "image_url": "/tmp/fake.png",
            "question": "What is in this image?",
        })

    assert resp.status_code == 200
    assert resp.json()["answer"] == "The image shows a tiger."


def test_analyze_bad_source_returns_422(vlm_client):
    with patch("vlm_scorer._call_vlm", side_effect=OSError("unreachable")):
        resp = vlm_client.post("/analyze", json={
            "image_url": "http://bad-host.example.com/img.png",
            "question": "What is this?",
        })

    assert resp.status_code == 422


# ── _call_vlm — llama.cpp backend (Linux / Windows) ──────────────────────────

def _llm_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.__getitem__.side_effect = lambda k: (
        [{"message": {"content": content}}] if k == "choices" else [][0]
    )
    return resp


@pytest.mark.skipif(_vlm_mod._IS_MACOS, reason="llama.cpp backend (Linux/Windows only)")
def test_call_vlm_llamacpp_local_file(tmp_path):
    fake_img = tmp_path / "fake.png"
    fake_img.write_bytes(b"\x89PNG")
    with patch("vlm_scorer._llm") as mock_llm:
        mock_llm.create_chat_completion.return_value = _llm_response("result")
        answer = _vlm_mod._call_vlm(str(fake_img), "test prompt", 64)
    assert answer == "result"


@pytest.mark.skipif(_vlm_mod._IS_MACOS, reason="llama.cpp backend (Linux/Windows only)")
def test_call_vlm_llamacpp_uses_temperature_zero(tmp_path):
    fake_img = tmp_path / "fake.png"
    fake_img.write_bytes(b"\x89PNG")
    with patch("vlm_scorer._llm") as mock_llm:
        mock_llm.create_chat_completion.return_value = _llm_response("result")
        _vlm_mod._call_vlm(str(fake_img), "test prompt", 64)
        kwargs = mock_llm.create_chat_completion.call_args[1]
        assert kwargs.get("temperature") == 0.0
        assert kwargs.get("stream") is False


@pytest.mark.skipif(_vlm_mod._IS_MACOS, reason="llama.cpp backend (Linux/Windows only)")
def test_call_vlm_llamacpp_http_url():
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = b"\x89PNG"
    mock_resp.headers.get.return_value = "image/png"
    with (
        patch("urllib.request.urlopen", return_value=mock_resp),
        patch("vlm_scorer._llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response("result")
        answer = _vlm_mod._call_vlm("http://example.com/img.png", "test", 64)
    assert answer == "result"


@pytest.mark.skipif(_vlm_mod._IS_MACOS, reason="llama.cpp backend (Linux/Windows only)")
def test_call_vlm_llamacpp_bad_http_raises_oserror():
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        with pytest.raises(OSError):
            _vlm_mod._call_vlm("http://bad-host.example.com/img.png", "test", 64)


# ── _call_vlm — mlx-vlm backend (macOS) ──────────────────────────────────────

@pytest.mark.skipif(not _vlm_mod._IS_MACOS, reason="mlx-vlm backend (macOS only)")
def test_call_vlm_mlx_local_file(tmp_path):
    fake_img = tmp_path / "fake.png"
    fake_img.write_bytes(b"\x89PNG")
    with patch("vlm_scorer._mlx_generate", return_value="result") as mock_gen:
        answer = _vlm_mod._call_vlm(str(fake_img), "test prompt", 64)
    assert answer == "result"
    assert mock_gen.call_args[1].get("temp") == 0.0
    assert mock_gen.call_args[1].get("verbose") is False


@pytest.mark.skipif(not _vlm_mod._IS_MACOS, reason="mlx-vlm backend (macOS only)")
def test_call_vlm_mlx_data_uri():
    import base64
    import io

    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
    data_uri = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"
    with patch("vlm_scorer._mlx_generate", return_value="result") as mock_gen:
        answer = _vlm_mod._call_vlm(data_uri, "test", 64)
    assert answer == "result"
    # data: URIs are decoded to a PIL Image before being passed to mlx-vlm
    assert isinstance(mock_gen.call_args[1].get("image"), PILImage.Image)
