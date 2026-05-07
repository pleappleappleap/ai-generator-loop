"""Tests for tactical_llm.py — budget helpers, heuristics, and verdict handler."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Constants used across tests ───────────────────────────────────────────────

_MAX_RETRIES  = 3
_MAX_INPAINTS = 2

BUDGET_FRESH = {
    "retries_used":  0,
    "inpaints_used": 0,
    "max_retries":   _MAX_RETRIES,
    "max_inpaints":  _MAX_INPAINTS,
}
BUDGET_RETRIES_EXHAUSTED = {
    "retries_used":  3,
    "inpaints_used": 0,
    "max_retries":   _MAX_RETRIES,
    "max_inpaints":  _MAX_INPAINTS,
}
BUDGET_ALL_EXHAUSTED = {
    "retries_used":  3,
    "inpaints_used": 2,
    "max_retries":   _MAX_RETRIES,
    "max_inpaints":  _MAX_INPAINTS,
}

CANDIDATE_VERDICT = {
    "image_uuid": "img-1",
    "verdict": "candidate",
    "scores": {
        "session_uuid":    "sess-1",
        "clip":            {"clip_score": 0.42},
        "artifact":        {"ai_confidence": 0.22},
        "vlm": {
            "photorealism":             8.5,
            "anatomical_coherence":     7.0,
            "interaction_plausibility": 7.5,
            "lighting_consistency":     8.0,
            "prompt_adherence":         9.0,
            "issues":          [],
            "recommendations": [],
        },
        "prompt": "a photorealistic tiger in the snow",
    },
}

REJECTED_VERDICT = {
    "image_uuid": "img-2",
    "verdict": "rejected",
    "reason": "clip_threshold",
    "scores": {
        "session_uuid": "sess-2",
        "clip":         {"clip_score": 0.18},
        "artifact":     {"ai_confidence": 0.30},
        "vlm":          {},
        "prompt":       "a woman at dusk",
    },
}


# ── _coordinator_call / _get_budget ──────────────────────────────────────────

class TestGetBudget:
    def test_returns_budget_when_found(self):
        import tactical_llm

        budget_resp = {
            "ok": True,
            "retries_used": 1, "inpaints_used": 0,
            "max_retries": 3, "max_inpaints": 2,
        }
        with patch.object(tactical_llm, "_coordinator_call", return_value=budget_resp):
            result = tactical_llm._get_budget("sess-1")

        assert result["retries_used"] == 1
        assert result["max_retries"] == 3

    def test_initialises_when_not_found(self):
        import tactical_llm

        not_found = {"ok": False, "error": "session not found"}
        init_resp  = {"ok": True}
        with patch.object(tactical_llm, "_coordinator_call",
                          side_effect=[not_found, init_resp]) as mock_call:
            result = tactical_llm._get_budget("sess-new")

        assert result["retries_used"] == 0
        assert result["max_retries"] == tactical_llm._MAX_RETRIES
        # Second call should be BudgetInit
        assert mock_call.call_args_list[1][0][0]["op"] == "BudgetInit"

    def test_returns_defaults_when_not_found(self):
        import tactical_llm

        not_found = {"ok": False, "error": "session not found"}
        init_resp  = {"ok": True}
        with patch.object(tactical_llm, "_coordinator_call",
                          side_effect=[not_found, init_resp]):
            result = tactical_llm._get_budget("sess-new")

        assert result["inpaints_used"] == 0
        assert result["max_inpaints"] == tactical_llm._MAX_INPAINTS


class TestIncrementBudget:
    def test_calls_budget_update(self):
        import tactical_llm

        with patch.object(tactical_llm, "_coordinator_call", return_value={"ok": True}) as mock_call:
            tactical_llm._increment_budget("sess-1", "retries_used")

        mock_call.assert_called_once_with({
            "op":           "BudgetUpdate",
            "session_uuid": "sess-1",
            "field":        "retries_used",
        })

    def test_calls_inpaints_update(self):
        import tactical_llm

        with patch.object(tactical_llm, "_coordinator_call", return_value={"ok": True}) as mock_call:
            tactical_llm._increment_budget("sess-1", "inpaints_used")

        assert mock_call.call_args[0][0]["field"] == "inpaints_used"


# ── _heuristic_decision ───────────────────────────────────────────────────────

class TestHeuristicDecision:
    def test_accept_when_scores_above_threshold(self):
        import tactical_llm

        verdict = {
            "verdict": "candidate",
            "scores": {
                "clip":     {"clip_score": 0.40},
                "vlm": {
                    "photorealism": 8.0, "anatomical_coherence": 8.0,
                    "interaction_plausibility": 8.0,
                    "lighting_consistency": 8.0, "prompt_adherence": 8.0,
                },
                "prompt": "a tiger",
            },
        }
        result = tactical_llm._heuristic_decision(verdict, BUDGET_FRESH)
        assert result["decision"] == "accept"

    def test_retry_rejected_with_budget(self):
        import tactical_llm

        result = tactical_llm._heuristic_decision(REJECTED_VERDICT, BUDGET_FRESH)
        assert result["decision"] == "retry"

    def test_give_up_rejected_with_exhausted_retries(self):
        import tactical_llm

        result = tactical_llm._heuristic_decision(REJECTED_VERDICT, BUDGET_RETRIES_EXHAUSTED)
        assert result["decision"] == "give_up"

    def test_retry_candidate_with_low_vlm_mean(self):
        import tactical_llm

        verdict = {
            "verdict": "candidate",
            "scores": {
                "clip": {"clip_score": 0.35},
                "vlm": {
                    "photorealism": 5.0, "anatomical_coherence": 5.0,
                    "interaction_plausibility": 5.0,
                    "lighting_consistency": 5.0, "prompt_adherence": 5.0,
                },
                "prompt": "a tiger",
            },
        }
        result = tactical_llm._heuristic_decision(verdict, BUDGET_FRESH)
        assert result["decision"] == "retry"

    def test_give_up_all_budgets_exhausted(self):
        import tactical_llm

        result = tactical_llm._heuristic_decision(REJECTED_VERDICT, BUDGET_ALL_EXHAUSTED)
        assert result["decision"] == "give_up"

    def test_decision_has_required_keys(self):
        import tactical_llm

        result = tactical_llm._heuristic_decision(CANDIDATE_VERDICT, BUDGET_FRESH)
        assert "decision" in result
        assert "reasoning" in result
        assert "confidence" in result


# ── _handle_verdict (integration of decision flow) ────────────────────────────

class TestHandleVerdict:
    """Tests for _handle_verdict with all external calls mocked."""

    def _setup_mocks(self, verdict, budget, decision):
        """Return a dict of patches needed to call _handle_verdict cleanly."""
        return {
            "_get_budget":          MagicMock(return_value=budget),
            "_increment_budget":    MagicMock(),
            "_publish_accepted":    MagicMock(),
            "_publish_retry":       MagicMock(return_value="new-img-uuid"),
            "_publish_inpaint":     MagicMock(),
            "_publish_decision_log": MagicMock(),
            "_run_inference":       MagicMock(return_value=decision),
        }

    def _call_with_mocks(self, verdict, budget, decision):
        import tactical_llm

        conn = MagicMock()
        mocks = self._setup_mocks(verdict, budget, decision)

        with patch.object(tactical_llm, "_get_budget",          mocks["_get_budget"]), \
             patch.object(tactical_llm, "_increment_budget",    mocks["_increment_budget"]), \
             patch.object(tactical_llm, "_publish_accepted",    mocks["_publish_accepted"]), \
             patch.object(tactical_llm, "_publish_retry",       mocks["_publish_retry"]), \
             patch.object(tactical_llm, "_publish_inpaint",     mocks["_publish_inpaint"]), \
             patch.object(tactical_llm, "_publish_decision_log", mocks["_publish_decision_log"]), \
             patch.object(tactical_llm, "_run_inference",       mocks["_run_inference"]), \
             patch("retrieval.session_history", return_value=[]), \
             patch("retrieval.similar_past",    return_value=[]), \
             patch("retrieval.embed_prompt",    return_value=[0.0] * 512):
            tactical_llm._handle_verdict(conn, json.dumps(verdict))

        return conn, mocks

    def test_accept_publishes_accepted(self):

        decision = {"decision": "accept", "confidence": 0.9, "reasoning": "great image"}
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_FRESH, decision)
        mocks["_publish_accepted"].assert_called_once()

    def test_accept_does_not_publish_retry(self):

        decision = {"decision": "accept", "confidence": 0.9, "reasoning": "great image"}
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_FRESH, decision)
        mocks["_publish_retry"].assert_not_called()

    def test_retry_publishes_retry_and_increments_budget(self):

        decision = {
            "decision": "retry", "confidence": 0.7,
            "reasoning": "low VLM", "retry_prompt": "revised prompt",
        }
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_FRESH, decision)
        mocks["_publish_retry"].assert_called_once()
        mocks["_increment_budget"].assert_called_once_with("sess-1", "retries_used")

    def test_retry_overrides_to_give_up_when_budget_exhausted(self):

        decision = {
            "decision": "retry", "confidence": 0.5,
            "reasoning": "low VLM", "retry_prompt": "revised prompt",
        }
        conn, mocks = self._call_with_mocks(
            CANDIDATE_VERDICT, BUDGET_RETRIES_EXHAUSTED, decision
        )
        mocks["_publish_retry"].assert_not_called()
        mocks["_increment_budget"].assert_not_called()

    def test_inpaint_downgrades_to_retry_when_stub(self):
        # inpaint is not yet implemented — always downgrades to retry when budget available

        decision = {
            "decision": "inpaint", "confidence": 0.75,
            "reasoning": "localised defect",
            "inpaint_regions": [{"description": "left hand", "issue": "extra finger",
                                  "correction": "correct anatomy"}],
            "inpaint_prompt": "correct the left hand",
        }
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_FRESH, decision)
        mocks["_publish_inpaint"].assert_not_called()
        mocks["_publish_retry"].assert_called_once()
        mocks["_increment_budget"].assert_called_once_with("sess-1", "retries_used")

    def test_inpaint_downgrades_to_retry_when_inpaint_budget_gone(self):

        budget = {**BUDGET_FRESH, "inpaints_used": 2}  # inpaints exhausted
        decision = {
            "decision": "inpaint", "confidence": 0.6,
            "reasoning": "localised defect",
        }
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, budget, decision)
        mocks["_publish_inpaint"].assert_not_called()
        mocks["_publish_retry"].assert_called_once()

    def test_inpaint_gives_up_when_all_budgets_gone(self):

        decision = {
            "decision": "inpaint", "confidence": 0.5,
            "reasoning": "localised defect",
        }
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_ALL_EXHAUSTED, decision)
        mocks["_publish_inpaint"].assert_not_called()
        mocks["_publish_retry"].assert_not_called()

    def test_give_up_publishes_no_generation_message(self):

        decision = {"decision": "give_up", "confidence": 1.0, "reasoning": "all budgets gone"}
        conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_FRESH, decision)
        mocks["_publish_accepted"].assert_not_called()
        mocks["_publish_retry"].assert_not_called()
        mocks["_publish_inpaint"].assert_not_called()

    def test_decision_log_always_published(self):

        for action in ("accept", "retry", "give_up"):
            decision = {
                "decision": action, "confidence": 0.9, "reasoning": "test",
                "retry_prompt": "p",
            }
            conn, mocks = self._call_with_mocks(CANDIDATE_VERDICT, BUDGET_FRESH, decision)
            mocks["_publish_decision_log"].assert_called_once()

    def test_heuristic_fallback_used_on_empty_llm_output(self):
        import tactical_llm

        conn = MagicMock()
        with patch.object(tactical_llm, "_get_budget", return_value=BUDGET_FRESH), \
             patch.object(tactical_llm, "_run_inference", return_value={}), \
             patch.object(tactical_llm, "_heuristic_decision",
                          return_value={"decision": "give_up", "reasoning": "heuristic",
                                        "confidence": 1.0}) as mock_heuristic, \
             patch.object(tactical_llm, "_publish_decision_log", MagicMock()), \
             patch("retrieval.session_history", return_value=[]), \
             patch("retrieval.similar_past",    return_value=[]), \
             patch("retrieval.embed_prompt",    return_value=[0.0] * 512):
            tactical_llm._handle_verdict(conn, json.dumps(CANDIDATE_VERDICT))

        mock_heuristic.assert_called_once()
