"""System prompt and decision prompt construction for the tactical LLM.

The tactical LLM receives the full scorer payload for a generated image and
decides whether to accept it, retry with a revised prompt, or request
localised inpainting.

Prompts are constructed as a single-turn user message appended to the system
prompt below. The model is expected to respond with a single JSON object and
nothing else, enabling reliable programmatic parsing.

Qwen3 hybrid thinking mode
---------------------------
Qwen3 supports a ``/think`` token that enables chain-of-thought reasoning
before the JSON response, and a ``/no_think`` token that suppresses it.
``build_decision_prompt`` appends the appropriate token automatically:

* ``/think`` — borderline cases where retries or inpaints remain and the
  scores are ambiguous. The model reasons through the diagnosis before
  committing to a decision.
* ``/no_think`` — unambiguous cases: budget exhausted (give_up is automatic)
  or all scores clearly above acceptance thresholds (clean accept).

When thinking is enabled the model produces a ``<think>…</think>`` block
before the JSON. ``_run_inference`` in ``tactical_llm.py`` strips this block
before parsing. The thinking budget is separate from ``max_tokens``; see
``tactical.model.max_tokens_thinking`` in ``config.yaml``.

Typical token budget:
    System prompt:         ~550 tokens
    Decision prompt:       ~300–600 tokens (scales with history depth)
    Thinking block:        0–2000 tokens (thinking mode only)
    Response:              ≤512 tokens (configured via tactical.model.max_tokens)
"""

from __future__ import annotations

import json

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


def _format_context(context: dict) -> str:
    """Format user chat messages and thumbs ratings for the prompt."""
    lines = []

    feedback = context.get("feedback", [])
    if feedback:
        lines.append("  User ratings for recent images:")
        for fb in feedback:
            rating = "👍 accepted" if fb.get("rating") == "up" else "👎 rejected"
            comment = f'  \u2014 \u201c{fb["comment"]}\u201d' if fb.get("comment") else ""
            lines.append(f"    {rating}{comment}  (image {fb.get('image_uuid', '?')[:8]}…)")
    else:
        lines.append("  User ratings: (none)")

    chat = context.get("chat", [])
    if chat:
        lines.append("")
        lines.append("  Recent conversation with user:")
        for msg in chat:
            role = "User" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")[:300]
            lines.append(f"    {role}: {content}")
    else:
        lines.append("  Recent chat: (none)")

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


def _should_think(verdict: dict, budget: dict) -> bool:
    """Return True if this decision warrants Qwen3 chain-of-thought reasoning.

    Thinking is suppressed for two unambiguous cases where it adds latency
    without benefit:

    1. Budget exhausted — give_up is the only possible outcome.
    2. All VLM scores clearly above acceptance thresholds with no issues and
       a strong CLIP score — unambiguous accept.

    Everything else (borderline scores, specific issues, retry/inpaint
    choices, prior failures in session history) warrants reasoning.

    Args:
        verdict: Full verdict dict from scorer.result.
        budget: Budget dict for this session.

    Returns:
        True if ``/think`` should be appended to the decision prompt.
    """
    retries_left  = budget["max_retries"]  - budget["retries_used"]
    inpaints_left = budget["max_inpaints"] - budget["inpaints_used"]
    if retries_left <= 0 and inpaints_left <= 0:
        return False

    scores = verdict.get("scores", {})
    vlm    = scores.get("vlm", {})
    clip   = scores.get("clip", {}).get("clip_score", 0.0)

    dims = [
        vlm.get("photorealism"),
        vlm.get("anatomical_coherence"),
        vlm.get("interaction_plausibility"),
        vlm.get("lighting_consistency"),
        vlm.get("prompt_adherence"),
    ]
    valid = [d for d in dims if d is not None]
    if (
        valid
        and all(d >= 8.0 for d in valid)
        and isinstance(clip, float) and clip >= 0.35
        and not vlm.get("issues")
    ):
        return False  # unambiguous accept — no reasoning needed

    return True


def build_decision_prompt(
    verdict: dict,
    session_history: list[dict],
    similar_past: list[dict],
    budget: dict,
    context: dict | None = None,
    enable_thinking: bool | None = None,
) -> str:
    """Construct the single-turn user message sent to the tactical LLM.

    Appends ``/think`` or ``/no_think`` to the end of the prompt to control
    Qwen3 hybrid thinking mode. Pass ``enable_thinking=True`` or ``False``
    to override auto-detection, or leave it as ``None`` to let
    :func:`_should_think` decide.

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
        context: Optional dict from the coordinator ``ContextGet`` response.
            Contains ``chat`` (list of recent conversation messages) and
            ``feedback`` (list of user thumbs ratings with optional comments).
            When provided, injected as a USER CONTEXT section so the LLM can
            weight its decision against the user's stated preferences.
        enable_thinking: Override thinking-mode selection. ``None`` (default)
            auto-detects via :func:`_should_think`.

    Returns:
        Formatted decision prompt string, ending with ``/think`` or
        ``/no_think``.
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
    ]

    if context:
        lines += [
            "── USER CONTEXT (ratings and chat for this workflow) ────────",
            _format_context(context),
            "",
        ]

    thinking = enable_thinking if enable_thinking is not None else _should_think(verdict, budget)
    lines.append("/think" if thinking else "/no_think")

    return "\n".join(lines)


def _get_prompt_from_verdict(verdict: dict, session_history: list[dict]) -> str:
    """Extract the prompt from the verdict payload or fall back to session history."""
    scores = verdict.get("scores", {})
    prompt = scores.get("prompt") or verdict.get("prompt")
    if prompt:
        return prompt
    if session_history:
        return session_history[-1].get("prompt", "(unknown)")
    return "(unknown)"
