"""Tests for prompts.py — build_decision_prompt and formatting helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from prompts import (  # noqa: E402
    SYSTEM_PROMPT,
    _format_history,
    _format_similar,
    _format_vlm,
    _get_prompt_from_verdict,
    build_decision_prompt,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

CANDIDATE_VERDICT = {
    "image_uuid": "img-1",
    "verdict": "candidate",
    "prompt": "a photorealistic tiger in the snow",
    "scores": {
        "clip":     {"clip_score": 0.42},
        "artifact": {"ai_confidence": 0.22},
        "vlm": {
            "photorealism":            8.5,
            "anatomical_coherence":    7.0,
            "interaction_plausibility": 7.5,
            "lighting_consistency":    8.0,
            "prompt_adherence":        9.0,
            "issues":          ["slight noise in background"],
            "recommendations": ["increase steps to 40"],
        },
        "prompt": "a photorealistic tiger in the snow",
    },
}

REJECTED_VERDICT = {
    "image_uuid": "img-2",
    "verdict": "rejected",
    "reason": "clip_threshold",
    "scores": {
        "clip":     {"clip_score": 0.18},
        "artifact": {"ai_confidence": 0.30},
        "vlm":      {},
        "prompt":   "a portrait of a woman at dusk",
    },
}

BUDGET_FULL = {"retries_used": 0, "inpaints_used": 0, "max_retries": 3, "max_inpaints": 2}
BUDGET_EXHAUSTED = {"retries_used": 3, "inpaints_used": 2, "max_retries": 3, "max_inpaints": 2}


# ── SYSTEM_PROMPT ─────────────────────────────────────────────────────────────

def test_system_prompt_contains_accept():
    assert "accept" in SYSTEM_PROMPT

def test_system_prompt_contains_retry():
    assert "retry" in SYSTEM_PROMPT

def test_system_prompt_contains_inpaint():
    assert "inpaint" in SYSTEM_PROMPT

def test_system_prompt_contains_give_up():
    assert "give_up" in SYSTEM_PROMPT

def test_system_prompt_contains_json_format():
    assert '"decision"' in SYSTEM_PROMPT


# ── _format_vlm ───────────────────────────────────────────────────────────────

def test_format_vlm_with_values():
    flat = {
        "vlm_photorealism": 8.5,
        "vlm_anatomical":   7.0,
        "vlm_interaction":  7.5,
        "vlm_lighting":     8.0,
        "vlm_prompt":       9.0,
    }
    result = _format_vlm(flat)
    assert "8.5" in result
    assert "photorealism" in result
    assert "prompt_adherence" in result


def test_format_vlm_missing_values_show_na():
    result = _format_vlm({})
    assert "n/a" in result


# ── _format_history ───────────────────────────────────────────────────────────

def test_format_history_empty():
    assert "(none)" in _format_history([])


def test_format_history_single_entry():
    history = [{
        "sequence_number": 1,
        "verdict": "candidate",
        "clip_score": 0.35,
        "vlm_mean": 7.2,
        "rejection_reason": None,
        "prompt": "a tiger",
        "vlm_recommendations": json.dumps(["use higher steps"]),
    }]
    result = _format_history(history)
    assert "seq=1" in result
    assert "candidate" in result
    assert "0.350" in result
    assert "7.2" in result
    assert "use higher steps" in result


def test_format_history_multiple_entries_ordered():
    history = [
        {"sequence_number": 1, "verdict": "rejected", "clip_score": 0.18,
         "vlm_mean": None, "rejection_reason": "clip_threshold",
         "prompt": "prompt 1", "vlm_recommendations": None},
        {"sequence_number": 2, "verdict": "candidate", "clip_score": 0.45,
         "vlm_mean": 7.8, "rejection_reason": None,
         "prompt": "prompt 2", "vlm_recommendations": None},
    ]
    result = _format_history(history)
    assert "seq=1" in result
    assert "seq=2" in result
    assert "clip_threshold" in result


# ── _format_similar ───────────────────────────────────────────────────────────

def test_format_similar_empty():
    assert "(none)" in _format_similar([])


def test_format_similar_with_entry():
    similar = [{
        "verdict": "accepted",
        "clip_score": 0.50,
        "vlm_mean": 8.1,
        "prompt": "a lion at sunset",
        "vlm_recommendations": json.dumps(["boost saturation"]),
    }]
    result = _format_similar(similar)
    assert "accepted" in result
    assert "0.500" in result
    assert "a lion at sunset" in result


# ── _get_prompt_from_verdict ──────────────────────────────────────────────────

def test_get_prompt_from_scores():
    verdict = {"scores": {"prompt": "from scores"}}
    assert _get_prompt_from_verdict(verdict, []) == "from scores"


def test_get_prompt_from_top_level():
    verdict = {"prompt": "top-level prompt", "scores": {}}
    assert _get_prompt_from_verdict(verdict, []) == "top-level prompt"


def test_get_prompt_falls_back_to_history():
    verdict = {"scores": {}}
    history = [{"prompt": "history prompt"}]
    assert _get_prompt_from_verdict(verdict, history) == "history prompt"


def test_get_prompt_unknown_when_no_data():
    assert _get_prompt_from_verdict({"scores": {}}, []) == "(unknown)"


# ── build_decision_prompt ─────────────────────────────────────────────────────

def test_build_prompt_candidate_contains_key_sections():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_FULL)
    assert "IMAGE UNDER EVALUATION" in result
    assert "SCORES" in result
    assert "BUDGET" in result
    assert "candidate" in result
    assert "0.42" in result


def test_build_prompt_rejected_contains_reason():
    result = build_decision_prompt(REJECTED_VERDICT, [], [], BUDGET_FULL)
    assert "clip_threshold" in result
    assert "rejected" in result


def test_build_prompt_budget_remaining_shown():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_FULL)
    assert "3 / 3" in result  # retries remaining
    assert "2 / 2" in result  # inpaints remaining


def test_build_prompt_exhausted_budget_shown():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_EXHAUSTED)
    assert "0 / 3" in result
    assert "0 / 2" in result


def test_build_prompt_includes_vlm_issues():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_FULL)
    assert "slight noise in background" in result


def test_build_prompt_includes_vlm_recommendations():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_FULL)
    assert "increase steps to 40" in result


def test_build_prompt_ends_with_think_token():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_FULL)
    stripped = result.strip()
    assert stripped.endswith("/think") or stripped.endswith("/no_think")


def test_build_prompt_with_history():
    history = [{
        "sequence_number": 1,
        "verdict": "rejected",
        "clip_score": 0.18,
        "vlm_mean": None,
        "rejection_reason": "clip_threshold",
        "prompt": "a photorealistic tiger in the snow",
        "vlm_recommendations": None,
    }]
    result = build_decision_prompt(CANDIDATE_VERDICT, history, [], BUDGET_FULL)
    assert "SESSION HISTORY" in result
    assert "seq=1" in result


def test_build_prompt_returns_string():
    result = build_decision_prompt(CANDIDATE_VERDICT, [], [], BUDGET_FULL)
    assert isinstance(result, str)
    assert len(result) > 100
