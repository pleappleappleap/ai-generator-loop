import json
from unittest.mock import MagicMock, mock_open, patch


def _llm_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.__getitem__.side_effect = lambda k: (
        [{"message": {"content": content}}] if k == "choices" else [][0]
    )
    return resp


_VALID_JSON = json.dumps({
    "photorealism": 8,
    "anatomical_coherence": 7,
    "interaction_plausibility": 6,
    "lighting_consistency": 9,
    "prompt_adherence": 8,
    "issues": ["minor blur"],
    "recommendations": ["increase sharpness"],
})


def test_health(vlm_client):
    resp = vlm_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_score_returns_expected_fields(vlm_client):
    with (
        patch("builtins.open", mock_open(read_data=b"\x89PNG")),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response(_VALID_JSON)
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
    with (
        patch("builtins.open", mock_open(read_data=b"\x89PNG")),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response(raw)
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
    with (
        patch("builtins.open", mock_open(read_data=b"\x89PNG")),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response("not valid json }")
        resp = vlm_client.post("/score", json={
            "image_uuid": "uuid-3",
            "image_path": "/tmp/fake.png",
            "prompt": "test",
        })

    assert resp.status_code == 422


def test_score_unreadable_image_returns_422(vlm_client):
    with patch("builtins.open", side_effect=OSError("no such file")):
        resp = vlm_client.post("/score", json={
            "image_uuid": "uuid-4",
            "image_path": "/nonexistent/img.png",
            "prompt": "test",
        })

    assert resp.status_code == 422


def test_score_uses_temperature_zero(vlm_client):
    with (
        patch("builtins.open", mock_open(read_data=b"\x89PNG")),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response(_VALID_JSON)
        vlm_client.post("/score", json={
            "image_uuid": "uuid-5",
            "image_path": "/tmp/fake.png",
            "prompt": "test",
        })
        call_kwargs = mock_llm.create_chat_completion.call_args[1]
        assert call_kwargs.get("temperature") == 0.0
        assert call_kwargs.get("stream") is False


# ── /analyze ──────────────────────────────────────────────────────────────────

def test_analyze_local_file_returns_answer(vlm_client):
    with (
        patch("builtins.open", mock_open(read_data=b"\x89PNG")),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response("The image shows a tiger.")
        resp = vlm_client.post("/analyze", json={
            "image_url": "/tmp/fake.png",
            "question": "What is in this image?",
        })

    assert resp.status_code == 200
    assert resp.json()["answer"] == "The image shows a tiger."


def test_analyze_data_uri_returns_answer(vlm_client):
    with patch("vlm_scorer.llm") as mock_llm:
        mock_llm.create_chat_completion.return_value = _llm_response("A landscape.")
        resp = vlm_client.post("/analyze", json={
            "image_url": "data:image/png;base64,iVBORw0KGgo=",
            "question": "Describe this.",
        })

    assert resp.status_code == 200
    assert resp.json()["answer"] == "A landscape."


def test_analyze_http_url_returns_answer(vlm_client):
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = b"\x89PNG"
    mock_response.headers.get.return_value = "image/png"
    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response("A cat.")
        resp = vlm_client.post("/analyze", json={
            "image_url": "http://example.com/image.png",
            "question": "What is this?",
        })

    assert resp.status_code == 200
    assert resp.json()["answer"] == "A cat."


def test_analyze_unreadable_local_file_returns_422(vlm_client):
    with patch("builtins.open", side_effect=OSError("no such file")):
        resp = vlm_client.post("/analyze", json={
            "image_url": "/nonexistent/img.png",
            "question": "What is this?",
        })

    assert resp.status_code == 422


def test_analyze_bad_http_url_returns_422(vlm_client):
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        resp = vlm_client.post("/analyze", json={
            "image_url": "http://bad-host.example.com/img.png",
            "question": "What is this?",
        })

    assert resp.status_code == 422


def test_analyze_uses_temperature_zero_and_no_stream(vlm_client):
    with (
        patch("builtins.open", mock_open(read_data=b"\x89PNG")),
        patch("vlm_scorer.llm") as mock_llm,
    ):
        mock_llm.create_chat_completion.return_value = _llm_response("Answer.")
        vlm_client.post("/analyze", json={
            "image_url": "/tmp/fake.png",
            "question": "test",
        })
        call_kwargs = mock_llm.create_chat_completion.call_args[1]
        assert call_kwargs.get("temperature") == 0.0
        assert call_kwargs.get("stream") is False
