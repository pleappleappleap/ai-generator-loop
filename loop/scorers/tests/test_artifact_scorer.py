from unittest.mock import patch


def test_health(artifact_client):
    resp = artifact_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_score_returns_ai_confidence(artifact_client):
    with patch("artifact_scorer.detector") as mock_det:
        mock_det.return_value = [
            {"label": "artificial", "score": 0.85},
            {"label": "real", "score": 0.15},
        ]
        resp = artifact_client.post("/score", json={
            "image_uuid": "uuid-1",
            "image_path": "/tmp/fake.png",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["image_uuid"] == "uuid-1"
    import pytest
    assert data["ai_confidence"] == pytest.approx(0.85)


def test_score_missing_artificial_label_returns_zero(artifact_client):
    with patch("artifact_scorer.detector") as mock_det:
        mock_det.return_value = [{"label": "real", "score": 1.0}]
        resp = artifact_client.post("/score", json={
            "image_uuid": "uuid-2",
            "image_path": "/tmp/fake.png",
        })

    assert resp.status_code == 200
    assert resp.json()["ai_confidence"] == 0.0


def test_score_clamps_above_one(artifact_client):
    with patch("artifact_scorer.detector") as mock_det:
        mock_det.return_value = [{"label": "artificial", "score": 1.5}]
        resp = artifact_client.post("/score", json={
            "image_uuid": "uuid-3",
            "image_path": "/tmp/fake.png",
        })

    assert resp.status_code == 200
    assert resp.json()["ai_confidence"] == 1.0
