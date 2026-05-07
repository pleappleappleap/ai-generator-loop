"""Tests for retrieval.py — session_history and similar_past."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

_SESS_1 = "00000000-0000-0000-0000-000000000001"
_SESS_2 = "00000000-0000-0000-0000-000000000002"
_SESS_OTHER = "00000000-0000-0000-0000-000000000020"
_SESS_CURRENT = "00000000-0000-0000-0000-000000000010"


def _make_loop_record(**kwargs) -> dict:
    """Return a minimal Loop record with defaults for all LanceDB columns."""
    defaults = {
        "image_uuid":         "img-1",
        "session_uuid":       _SESS_1,
        "sequence_number":    1,
        "created_at":         "2026-01-01T00:00:00+00:00",
        "prompt":             "a tiger",
        "workflow_path":      "/workflows/default.json",
        "workflow_params":    "{}",
        "clip_score":         0.40,
        "artifact_score":     0.20,
        "vlm_photorealism":   8.0,
        "vlm_anatomical":     7.5,
        "vlm_interaction":    7.0,
        "vlm_lighting":       8.5,
        "vlm_prompt":         9.0,
        "vlm_issues":         "[]",
        "vlm_recommendations": "[]",
        "verdict":            "candidate",
        "rejection_reason":   None,
        "image_path":         "/images/img-1.png",
    }
    defaults.update(kwargs)
    return defaults


# ── session_history ───────────────────────────────────────────────────────────

class TestSessionHistory:
    """Tests for retrieval.session_history using a mocked LanceDB table."""

    def test_empty_result_when_no_records(self):
        """session_history returns [] when no rows match session_uuid."""
        import retrieval

        mock_table = MagicMock()
        mock_table.search.return_value.where.return_value \
            .to_list.return_value = []
        mock_db = MagicMock()
        mock_db.list_tables.return_value = ["loop"]
        mock_db.open_table.return_value = mock_table

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.session_history("00000000-0000-0000-0000-000000000099")

        assert result == []

    def test_returns_records_sorted_by_sequence(self):
        """session_history returns records ordered by sequence_number."""
        import retrieval

        rec1 = _make_loop_record(image_uuid="img-1", sequence_number=1)
        rec2 = _make_loop_record(image_uuid="img-2", sequence_number=2)
        # Returned in reverse order to verify sorting
        raw_records = [rec2, rec1]

        mock_table = MagicMock()
        mock_table.search.return_value.where.return_value \
            .to_list.return_value = raw_records
        mock_db = MagicMock()
        mock_db.list_tables.return_value = ["loop"]
        mock_db.open_table.return_value = mock_table

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.session_history(_SESS_1)

        assert len(result) == 2
        seqs = [r["sequence_number"] for r in result]
        assert seqs == sorted(seqs)

    def test_vlm_mean_computed(self):
        """session_history adds a vlm_mean field to each record."""
        import retrieval

        rec = _make_loop_record(
            vlm_photorealism=8.0,
            vlm_anatomical=7.0,
            vlm_interaction=7.0,
            vlm_lighting=8.0,
            vlm_prompt=9.0,
        )
        mock_table = MagicMock()
        mock_table.search.return_value.where.return_value \
            .to_list.return_value = [rec]
        mock_db = MagicMock()
        mock_db.list_tables.return_value = ["loop"]
        mock_db.open_table.return_value = mock_table

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.session_history(_SESS_1)

        assert "vlm_mean" in result[0]
        expected_mean = (8.0 + 7.0 + 7.0 + 8.0 + 9.0) / 5
        assert abs(result[0]["vlm_mean"] - expected_mean) < 1e-6

    def test_returns_empty_list_when_table_absent(self):
        """session_history returns [] gracefully if the loop table doesn't exist."""
        import retrieval

        mock_db = MagicMock()
        mock_db.list_tables.return_value = []  # no tables

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.session_history(_SESS_1)

        assert result == []


# ── similar_past ──────────────────────────────────────────────────────────────

class TestSimilarPast:
    """Tests for retrieval.similar_past using mocked LanceDB ANN search."""

    def test_empty_result_when_no_table(self):
        """similar_past returns [] if the loop table doesn't exist."""
        import retrieval

        mock_db = MagicMock()
        mock_db.list_tables.return_value = []

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.similar_past([0.0] * 512, _SESS_1)

        assert result == []

    def test_excludes_current_session(self):
        """similar_past does not return records from the current session_uuid."""
        import retrieval

        # Two records: one from current session (should be excluded), one from other
        _make_loop_record(session_uuid=_SESS_1, image_uuid="img-a")
        rec_other   = _make_loop_record(session_uuid=_SESS_2, image_uuid="img-b")

        mock_table = MagicMock()
        # The WHERE clause filters out the current session — mock returns only the other record.
        mock_table.search.return_value.where.return_value \
            .limit.return_value.to_list.return_value = [rec_other]
        mock_db = MagicMock()
        mock_db.list_tables.return_value = ["loop"]
        mock_db.open_table.return_value = mock_table

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.similar_past([0.0] * 512, _SESS_1)

        uuids = [r["session_uuid"] for r in result]
        assert _SESS_1 not in uuids

    def test_vlm_mean_computed_for_similar(self):
        """similar_past adds vlm_mean to returned records."""
        import retrieval

        rec = _make_loop_record(
            session_uuid=_SESS_OTHER,
            vlm_photorealism=6.0,
            vlm_anatomical=6.0,
            vlm_interaction=6.0,
            vlm_lighting=6.0,
            vlm_prompt=6.0,
        )
        mock_table = MagicMock()
        mock_table.search.return_value.where.return_value \
            .limit.return_value.to_list.return_value = [rec]
        mock_db = MagicMock()
        mock_db.list_tables.return_value = ["loop"]
        mock_db.open_table.return_value = mock_table

        with patch("lancedb.connect", return_value=mock_db):
            result = retrieval.similar_past([0.0] * 512, _SESS_CURRENT)

        assert len(result) == 1
        assert abs(result[0]["vlm_mean"] - 6.0) < 1e-6


# ── embed_prompt ──────────────────────────────────────────────────────────────

class TestEmbedPrompt:
    """Tests for retrieval.embed_prompt output shape."""

    def test_returns_list_of_floats(self):
        """embed_prompt returns a list of floats."""
        import retrieval

        fake_embedding = [0.1] * 512
        with patch.object(retrieval, "_model") as mock_model, \
             patch.object(retrieval, "_tokenizer") as mock_tokenizer:
            mock_tokenizer.return_value = MagicMock()

            feature_tensor = MagicMock()
            feature_tensor.__truediv__ = lambda s, o: feature_tensor
            feature_tensor.norm = MagicMock(return_value=MagicMock())
            feature_tensor.squeeze = MagicMock(return_value=MagicMock(
                tolist=MagicMock(return_value=fake_embedding)
            ))
            mock_model.encode_text = MagicMock(return_value=feature_tensor)

            with patch("torch.no_grad"):
                result = retrieval.embed_prompt("a tiger in the snow")

        assert isinstance(result, list)
