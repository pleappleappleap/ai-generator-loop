from unittest.mock import patch

import torch


def _unit_feat() -> torch.Tensor:
    """512-d unit vector — dot product with itself == 1.0."""
    t = torch.ones(1, 512)
    return t / t.norm(dim=-1, keepdim=True)


def test_health(clip_client):
    resp = clip_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_score_returns_expected_fields(clip_client):
    feat = _unit_feat()
    with (
        patch("clip_scorer.Image"),
        patch("clip_scorer.preprocess") as mock_pre,
        patch("clip_scorer.model") as mock_model,
        patch("clip_scorer.tokenizer") as mock_tok,
    ):
        mock_pre.return_value.unsqueeze.return_value.to.return_value = feat
        mock_model.encode_image.return_value = feat
        mock_model.encode_text.return_value = feat
        mock_tok.return_value = torch.zeros(1, 77, dtype=torch.long)

        resp = clip_client.post("/score", json={
            "image_uuid": "uuid-1",
            "image_path": "/tmp/fake.png",
            "prompt": "a cat",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["image_uuid"] == "uuid-1"
    assert isinstance(data["clip_score"], float)
    assert -1.0 <= data["clip_score"] <= 1.0
    assert isinstance(data["image_embedding"], list)
    assert len(data["image_embedding"]) == 512
    assert isinstance(data["prompt_embedding"], list)
    assert len(data["prompt_embedding"]) == 512


def test_score_unit_vectors_give_similarity_one(clip_client):
    feat = _unit_feat()
    with (
        patch("clip_scorer.Image"),
        patch("clip_scorer.preprocess") as mock_pre,
        patch("clip_scorer.model") as mock_model,
        patch("clip_scorer.tokenizer") as mock_tok,
    ):
        mock_pre.return_value.unsqueeze.return_value.to.return_value = feat
        mock_model.encode_image.return_value = feat.clone()
        mock_model.encode_text.return_value = feat.clone()
        mock_tok.return_value = torch.zeros(1, 77, dtype=torch.long)

        resp = clip_client.post("/score", json={
            "image_uuid": "uuid-2",
            "image_path": "/tmp/fake.png",
            "prompt": "a cat",
        })

    assert resp.status_code == 200
    import pytest
    assert resp.json()["clip_score"] == pytest.approx(1.0, abs=1e-5)


def test_score_unreadable_image_returns_422(clip_client):
    with (
        patch("clip_scorer.preprocess") as mock_pre,
        patch("clip_scorer.Image"),
    ):
        mock_pre.side_effect = OSError("no such file")

        resp = clip_client.post("/score", json={
            "image_uuid": "uuid-3",
            "image_path": "/nonexistent/img.png",
            "prompt": "a cat",
        })

    assert resp.status_code == 422
