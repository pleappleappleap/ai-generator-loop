"""System prompt and decision prompt construction for the tactical LLM.

The tactical LLM receives the full scorer payload for a generated image and
decides whether to accept it, retry with a revised prompt, or request
localised inpainting.

Prompts are constructed as a single-turn user message appended to the system
prompt below. The model is expected to respond with a single JSON object and
nothing else, enabling reliable programmatic parsing.

Typical token budget:
    System prompt:      ~550 tokens
    Decision prompt:    ~300–600 tokens (scales with history depth)
    Response:           ≤512 tokens (configured via tactical.model.max_tokens)
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """\
You are the tactical decision engine for an autonomous SDXL image generation pipeline.

Your role is to evaluate scorer output for a generated image and decide one of three actions:
  accept   — the image meets quality standards; mark it as accepted
  retry    — regenerate with a revised prompt or parameters
  inpaint  — the overall composition is good but specific regions need correction

────────────────────────────────────────────────────────
SCORER OUTPUTS
────────────────────────────────────────────────────────

CLIP score (0–1): Cosine similarity between the image and its prompt using CLIP ViT-L-14.
Measures semantic alignment. Below 0.30 suggests meaningful prompt mismatch.

Artifact confidence (0–1): Probability that the image is detectably AI-generated.
Lower is more photorealistic. Above 0.50 may indicate visible compositing artifacts.

VLM scores (0–10 each):
  photorealism          How convincingly photographic the image appears
  anatomical_coherence  Correctness of human / animal anatomy
  interaction_plausibility  Believability of interactions between depicted elements
  lighting_consistency  Internal coherence of the lighting model across the image
  prompt_adherence      How faithfully the image reflects the original prompt

VLM also provides:
  issues           List of specific observed problems (strings)
  recommendations  List of specific prompt or parameter adjustments (strings)

Rejection reason (if pre-filtered by the aggregator):
  clip_threshold      Image was rejected before VLM scoring due to low CLIP score
  artifact_threshold  Image was rejected after CLIP scoring due to high artifact confidence
  null                Image passed all thresholds (candidate verdict)

────────────────────────────────────────────────────────
DECISION CRITERIA
────────────────────────────────────────────────────────

accept when:
  - Verdict is "candidate" (passed aggregator thresholds)
  - All VLM dimensions ≥ 7, or only minor non-structural issues present
  - CLIP score ≥ 0.30
  - Artifact confidence < 0.50

retry when:
  - Verdict is "rejected" with rejection_reason "clip_threshold"
    → the prompt may not match the checkpoint's style; revise it
  - Verdict is "rejected" with rejection_reason "artifact_threshold"
    → suggest lower CFG scale, more steps, or different sampler params
  - Verdict is "candidate" but VLM prompt_adherence < 6
  - Verdict is "candidate" but mean VLM score < 7.0
  - VLM recommendations suggest specific prompt changes
  - Inpainting would not fix the root cause of the problem
  - Retry budget has not been exhausted

inpaint when:
  - Verdict is "candidate"
  - Overall composition is good (most VLM dimensions ≥ 7)
  - One or two spatially bounded regions have specific, correctable defects
    (e.g. malformed hand, wrong garment colour, mismatched shadow direction)
  - Regeneration would likely fix the localised issue but risk harming the rest
  - Inpaint budget has not been exhausted

give_up when:
  - Both retry and inpaint budgets are exhausted
  - The systematic failure mode cannot be corrected by prompt revision alone

────────────────────────────────────────────────────────
OUTPUT FORMAT
────────────────────────────────────────────────────────

Respond with a single JSON object and absolutely nothing else — no markdown
fences, no preamble, no trailing commentary.

{
  "decision":      "accept" | "retry" | "inpaint" | "give_up",
  "reasoning":     "<one or two sentences>",
  "confidence":    <0.0–1.0>,
  "retry_prompt":  "<revised full prompt>",           // required if decision == retry
  "retry_params":  {},                                 // optional workflow param overrides
  "inpaint_regions": [                                 // required if decision == inpaint
    {
      "description":  "<region, e.g. 'left hand'>",
      "issue":        "<specific defect observed>",
      "correction":   "<what the inpaint should achieve>"
    }
  ],
  "inpaint_prompt": "<inpaint-specific prompt>"        // required if decision == inpaint
}
"""


def _format_vlm(scores: dict) -> str:
    """Format VLM score dimensions as a compact table."""
    dims = [
        ("photorealism",           scores.get("vlm_photorealism",  "n/a")),
        ("anatomical_coherence",   scores.get("vlm_anatomical",    "n/a")),
        ("interaction_plausibility", scores.get("vlm_interaction", "n/a")),
        ("lighting_consistency",   scores.get("vlm_lighting",      "n/a")),
        ("prompt_adherence",       scores.get("vlm_prompt",        "n/a")),
    ]
    lines = [f"  {name:<28} {val}" for name, val in dims]
    return "\n".join(lines)


def _format_history(history: list[dict]) -> str:
    """Format prior-session history entries for the prompt."""
    if not history:
        return "  (none)"
    lines = []
    for i, rec in enumerate(history, 1):
        vlm_mean = rec.get("vlm_mean")
        mean_str = f"{vlm_mean:.1f}" if vlm_mean is not None else "n/a"
        lines.append(
            f"  [{i}] seq={rec.get('sequence_number', '?')}  "
            f"verdict={rec.get('verdict', '?')}  "
            f"clip={rec.get('clip_score', 'n/a'):.3f}  "
            f"vlm_mean={mean_str}"
        )
        if rec.get("rejection_reason"):
            lines.append(f"       rejection_reason={rec['rejection_reason']}")
        if rec.get("prompt"):
            lines.append(f"       prompt: {rec['prompt'][:120]}")
        if rec.get("vlm_recommendations"):
            recs = json.loads(rec["vlm_recommendations"])
            for r in recs[:3]:
                lines.append(f"       rec: {r}")
    return "\n".join(lines)


def _format_similar(similar: list[dict]) -> str:
    """Format similar past sessions for the prompt."""
    if not similar:
        return "  (none)"
    lines = []
    for i, rec in enumerate(similar, 1):
        vlm_mean = rec.get("vlm_mean")
        mean_str = f"{vlm_mean:.1f}" if vlm_mean is not None else "n/a"
        lines.append(
            f"  [{i}] verdict={rec.get('verdict', '?')}  "
            f"clip={rec.get('clip_score', 'n/a'):.3f}  "
            f"vlm_mean={mean_str}"
        )
        if rec.get("prompt"):
            lines.append(f"       prompt: {rec['prompt'][:120]}")
        if rec.get("vlm_recommendations"):
            recs = json.loads(rec["vlm_recommendations"])
            for r in recs[:2]:
                lines.append(f"       rec: {r}")
    return "\n".join(lines)


def build_decision_prompt(
    verdict: dict,
    session_history: list[dict],
    similar_past: list[dict],
    budget: dict,
) -> str:
    """Construct the single-turn user message sent to the tactical LLM.

    Args:
        verdict: The full verdict dict from the ``scorer.result`` queue.
            Must contain at minimum ``image_uuid``, ``verdict``, and
            ``scores``. Scores dict should include ``clip``, ``artifact``,
            and ``vlm`` sub-dicts with their respective fields.
        session_history: Ordered list of prior Loop records for this
            ``session_uuid``. Each entry is a flat dict with schema-level
            field names plus a computed ``vlm_mean`` float.
        similar_past: List of similar past Loop records from other sessions,
            retrieved by ANN search on prompt embedding.
        budget: Dict with ``retries_used``, ``max_retries``,
            ``inpaints_used``, ``max_inpaints``.

    Returns:
        Formatted decision prompt string.
    """
    scores = verdict.get("scores", {})
    clip = scores.get("clip", {})
    artifact = scores.get("artifact", {})
    vlm = scores.get("vlm", {})

    clip_score       = clip.get("clip_score",       "n/a")
    ai_confidence    = artifact.get("ai_confidence", "n/a")
    rejection_reason = verdict.get("reason")
    verdict_type     = verdict.get("verdict", "unknown")
    image_uuid       = verdict.get("image_uuid", "unknown")

    issues_list = vlm.get("issues", [])
    recs_list   = vlm.get("recommendations", [])

    # Synthesise a flat scores dict for VLM formatter
    flat_scores = {
        "vlm_photorealism": vlm.get("photorealism"),
        "vlm_anatomical":   vlm.get("anatomical_coherence"),
        "vlm_interaction":  vlm.get("interaction_plausibility"),
        "vlm_lighting":     vlm.get("lighting_consistency"),
        "vlm_prompt":       vlm.get("prompt_adherence"),
    }

    prompt_text = _get_prompt_from_verdict(verdict, session_history)

    retries_remaining  = budget["max_retries"]  - budget["retries_used"]
    inpaints_remaining = budget["max_inpaints"] - budget["inpaints_used"]

    # Format clip_score for display
    clip_str = f"{clip_score:.4f}" if isinstance(clip_score, float) else str(clip_score)
    conf_str = f"{ai_confidence:.4f}" if isinstance(ai_confidence, float) else str(ai_confidence)

    lines = [
        "── IMAGE UNDER EVALUATION ───────────────────────────────────",
        f"  image_uuid:        {image_uuid}",
        f"  verdict:           {verdict_type}",
        f"  rejection_reason:  {rejection_reason or 'null'}",
        "",
        "── SCORES ───────────────────────────────────────────────────",
        f"  CLIP score:        {clip_str}",
        f"  Artifact conf:     {conf_str}",
        "",
        "  VLM dimensions:",
        _format_vlm(flat_scores),
        "",
        "  VLM issues:",
    ]
    if issues_list:
        for issue in issues_list:
            lines.append(f"    - {issue}")
    else:
        lines.append("    (none)")

    lines += [
        "",
        "  VLM recommendations:",
    ]
    if recs_list:
        for rec in recs_list:
            lines.append(f"    - {rec}")
    else:
        lines.append("    (none)")

    lines += [
        "",
        "── ORIGINAL PROMPT ──────────────────────────────────────────",
        f"  {prompt_text}",
        "",
        "── SESSION HISTORY (this session, ordered) ──────────────────",
        _format_history(session_history),
        "",
        "── SIMILAR PAST RESULTS (other sessions) ────────────────────",
        _format_similar(similar_past),
        "",
        "── BUDGET ───────────────────────────────────────────────────",
        f"  Retries remaining:  {retries_remaining} / {budget['max_retries']}",
        f"  Inpaints remaining: {inpaints_remaining} / {budget['max_inpaints']}",
        "",
        "Decide now. Output JSON only.",
    ]

    return "\n".join(lines)


def _get_prompt_from_verdict(verdict: dict, session_history: list[dict]) -> str:
    """Extract the prompt from the verdict payload or fall back to session history."""
    scores = verdict.get("scores", {})
    # The comfyui_worker embeds the prompt in the loop.complete message
    # which propagates through router → scorers → aggregator → scorer.result
    prompt = scores.get("prompt") or verdict.get("prompt")
    if prompt:
        return prompt
    # Fall back to the most recent session history entry
    if session_history:
        return session_history[-1].get("prompt", "(unknown)")
    return "(unknown)"
