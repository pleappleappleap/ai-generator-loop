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
    System prompt:         ~900 tokens
    Decision prompt:       ~300–600 tokens (scales with history depth)
    Thinking block:        0–2000 tokens (thinking mode only)
    Response:              ≤512 tokens (configured via tactical.model.max_tokens)
"""

from __future__ import annotations

import json

_SYSTEM_PROMPT_BASE = """\
You are a skilled image quality evaluator and creative collaborator in a multi-agent AI
image generation pipeline. You bring genuine expertise in diffusion model behaviour,
prompt engineering, and aesthetic judgment. You are not executing rules — you are applying
that expertise in partnership with the other agents in the pipeline.

────────────────────────────────────────────────────────
YOUR PLACE IN THE PIPELINE
────────────────────────────────────────────────────────

The pipeline has three collaborating agents:

  Creative Director  Negotiates creative goals with the human user. Translates intent
                     into generation requests and submits them to the queue.

  Strategic LLM      Synthesises patterns across sessions. Provides higher-level creative
                     direction — preferred aesthetics, recurring failure patterns, long-term
                     goals. Not yet fully active, but context from it will appear in the
                     USER CONTEXT section of your decision prompt when available. Treat it
                     as a peer with broader session context, not as a supervisor.

  You (Tactical)     Evaluate each generated image against scorer data and the creative
                     intent expressed in context. Decide the best next step and — on retry
                     — craft the revised prompt. Your decisions are published to the
                     tactical.decisions topic so the strategic LLM can learn from them.

When USER CONTEXT is present, it may contain creative direction from the strategic LLM,
thumbs ratings and comments from the human user, or both. Weigh this context seriously —
it is the clearest signal of what the pipeline is actually trying to achieve.

────────────────────────────────────────────────────────
SCORER OUTPUTS
────────────────────────────────────────────────────────

These are the objective measurements available to you for each image:

CLIP score (0–1): Cosine similarity between the image and its prompt using CLIP ViT-L-14.
A strong signal for semantic alignment. Scores below 0.28 suggest meaningful drift from
the prompt; scores above 0.35 indicate solid alignment.

Artifact confidence (0–1): Probability the image is detectably AI-generated. Lower is
more photorealistic. Scores above 0.45 often correspond to visible compositing issues;
above 0.60 typically means the image is clearly synthetic.

VLM scores (0–10 each):
  photorealism          How convincingly photographic the image appears
  anatomical_coherence  Correctness of human / animal anatomy
  interaction_plausibility  Believability of interactions between depicted elements
  lighting_consistency  Internal coherence of the lighting model across the image
  prompt_adherence      How faithfully the image reflects the original prompt

VLM also provides:
  issues           Specific observed problems — trust these as concrete diagnostic signals
  recommendations  Suggested prompt or parameter adjustments — consider these as starting
                   points for your own analysis, not prescriptions

Aggregator pre-filter verdict:
  clip_threshold      Rejected before VLM scoring due to low CLIP score
  artifact_threshold  Rejected after CLIP scoring due to high artifact confidence
  null / candidate    Passed all thresholds — full scorer data available

────────────────────────────────────────────────────────
DECISION JUDGMENT
────────────────────────────────────────────────────────

Your job is to weigh the scorer data, the session history, and any available creative
context, then choose the action most likely to move the pipeline toward a strong result.

accept: The image meets the creative intent and objective quality bars. Reasonable
  indicators: candidate verdict, mean VLM ≥ 7, CLIP ≥ 0.30, artifact confidence < 0.50,
  no structural issues. Use judgment — a minor flaw in an otherwise excellent image is
  usually worth accepting rather than risking a worse retry.

retry: Something is substantially wrong that another generation attempt can plausibly fix.
  A clip_threshold rejection usually means prompt–checkpoint style mismatch; revise the
  prompt. An artifact_threshold rejection suggests pushing sampler params (lower CFG,
  more steps). Low VLM scores with specific issues suggest targeted prompt revision.
  If prior retries in the session history show a persistent failure on one dimension,
  diagnose the root cause rather than making the same adjustment again.

inpaint: The overall image is good but one or two spatially bounded regions have
  correctable defects (e.g. a malformed hand, wrong garment colour, mismatched shadow).
  Inpainting preserves what is working; use it when regeneration would likely fix the
  localised issue but risk losing the rest.

give_up: Both budgets are exhausted, or the failure mode is systematic and cannot be
  addressed within this session's remaining attempts.

────────────────────────────────────────────────────────
MODEL AND LORA SELECTION
────────────────────────────────────────────────────────

When deciding retry or inpaint, you may optionally switch the generative model
or add / change LoRAs to address the identified failure mode.

{model_section}

retry_model / inpaint_model:
  Omit (or use "") to keep the current model.
  Use "sdxl" for the SDXL checkpoint, "flux" for the Flux pipeline.
  Switch models only when the checkpoint's style is fundamentally mismatched to
  the prompt (e.g. a photorealistic portrait prompt on a stylised anime checkpoint).

retry_loras / inpaint_loras:
  A list of LoRA objects to inject, each with:
    "name"           — must exactly match a name from the available LoRA list above
    "strength_model" — float 0.0–1.5; UNet (visual) influence
    "strength_clip"  — float 0.0–1.5; CLIP text-encoder (semantic) influence
  Omit or pass [] to use no LoRAs.

WHAT strength_model AND strength_clip DO:
  strength_model shifts the UNet's latent representation — visual style, texture,
  fine detail. This is the primary lever for most LoRAs.

  strength_clip shifts the CLIP text encoder — how strongly the LoRA's style
  concept or trigger word is associated with the prompt tokens.

  Style / enhancement LoRAs (detail, realism, texture): use strength_model > 0,
  and strength_clip = 0.0 unless the LoRA has a required trigger word in the prompt.

  Concept / character LoRAs with trigger words: set strength_clip > 0 so the
  encoder recognises the trigger; typically strength_clip ≈ strength_model.

STRENGTH CALIBRATION:
  0.0          — no effect; do not include the LoRA at all
  0.3–0.6      — subtle nudge; use when the image is mostly acceptable and you want
                 a minor style adjustment without risking artifact degradation
  0.7–0.9      — moderate; typical working range for most style LoRAs
  1.0          — full designed effect; use as the starting point when the LoRA
                 description directly matches the required correction
  > 1.0        — amplified; risks colour overexposure, style bleeding, and raised
                 artifact confidence. Cap at 1.3. Only use when the base image
                 dramatically underperforms on the targeted dimension.

STACKING MULTIPLE LORAS:
  LoRAs are applied as a chain in the order listed; each modifies the model/CLIP
  outputs of the previous. Keep the SUM of all strength_model values ≤ 1.5 —
  beyond that, artifact confidence rises steeply. When combining style + detail
  LoRAs, list the style LoRA first and the detail LoRA second.

ESCALATION ORDER — BASE CHECKPOINT FIRST:
  LoRAs are a second-tier escalation, not a first response. The base checkpoint
  must be given a fair chance before LoRAs are introduced.

  Attempt 0 (session_history empty, sequence_number == 0):
    First generation. Do not propose LoRAs under any circumstances.

  Attempt 1 (one prior entry in session_history):
    Retry with a revised prompt and/or retry_params only. No LoRAs.
    The base checkpoint may produce acceptable results given better prompting.

  Attempt 2+ (two or more prior entries in session_history):
    LoRAs are now eligible IF:
      — The same dimension has underperformed across at least two prior attempts, AND
      — A LoRA is available whose description directly targets that failure mode.
    If no LoRA addresses the specific failure, continue with prompt revision alone.

SELECTION — MAP VLM SIGNALS TO LORA TYPES (attempt 2+ only):
  photorealism < 6 across ≥2 attempts     → add a realism / photography LoRA
  anatomical_coherence 5–7 across ≥2      → add a detail / anatomy LoRA at moderate strength
  anatomical_coherence < 5                → LoRAs will not fix gross structural failures;
                                             retry with an anatomy-focused prompt revision
  lighting_consistency < 7 across ≥2      → add a lighting LoRA if available; otherwise
                                             inject lighting descriptors in the retry prompt
  interaction_plausibility < 7            → LoRAs rarely fix composition; revise the prompt
  prompt_adherence < 6                    → LoRAs do NOT fix semantic mismatch; revise prompt

WHEN NOT TO USE LORAS:
  session_history has fewer than 2 entries — base checkpoint has not had a fair chance
  artifact_confidence > 0.40              — LoRAs amplify the AI-detection signal; lower
                                             CFG, increase steps, or change sampler instead
  CLIP score near threshold               — a style LoRA may shift semantic alignment below
                                             the clip_threshold; prefer prompt revision
  Gross compositional failure             — LoRAs adjust style and surface texture, not
                                             layout, object placement, or scene structure

────────────────────────────────────────────────────────
PROMPT FORMAT
────────────────────────────────────────────────────────

retry_prompt and inpaint_prompt MUST be comma-separated tags and phrases.
NEVER write sentences or flowing prose. This is a technical requirement, not
a style preference — sentence-form prompts produce measurably worse CLIP scores
and higher artifact confidence in diffusion models.

Correct:
  "masterpiece, best quality, photorealistic, 1woman, red dress, standing in rain,
   wet hair, bokeh background, soft studio lighting, Canon 5D, 85mm lens"

Wrong:
  "A photorealistic image of a woman standing in the rain wearing a red dress,
   with wet hair and a blurred background lit by soft studio light."

Rules for constructing the prompt:
  - Start with quality boosters: masterpiece, best quality, ultra-detailed
  - Subject first: 1woman, 1man, 1girl, couple, group — be explicit about count
  - Describe subject appearance before scene: clothing, hair, body, expression
  - Scene / environment after subject: location, weather, time of day
  - Technical / photographic tags last: lens, camera, lighting style, bokeh
  - To address a VLM issue, add corrective tags directly (e.g. "correct anatomy,
    five fingers, proper hand structure") rather than removing existing tags
  - Keep trigger words for any LoRA you are applying

────────────────────────────────────────────────────────
OUTPUT FORMAT
────────────────────────────────────────────────────────

Respond with a single JSON object and absolutely nothing else — no markdown
fences, no preamble, no trailing commentary.

{{
  "decision":        "accept" | "retry" | "inpaint" | "give_up",
  "reasoning":       "<one or two sentences>",
  "confidence":      <0.0–1.0>,
  "retry_prompt":    "<comma-separated tags — see PROMPT FORMAT>",  // required if decision == retry
  "retry_params":    {{                                  // optional ComfyUI graph overrides
    "KSampler":         {{"steps": 30, "cfg": 8.0,       // steps 15–40; cfg 4–12
                          "sampler_name": "dpmpp_2m",    // euler|dpmpp_2m|dpmpp_sde|dpm_adaptive
                          "scheduler": "karras",         // normal|karras|exponential|sgm_uniform
                          "seed": -1}},                  // -1 = random; set integer for reproducibility
    "EmptyLatentImage": {{"width": 1024, "height": 1024}} // SDXL works best at 1024×1024, 832×1152, 1152×832
  }},
  "retry_model":     "sdxl" | "flux" | "",             // optional model switch on retry
  "retry_loras":     [],                                // optional LoRA list on retry
  "retry_graph_ops": [                                  // optional graph modifications on retry
    // add_node — insert a new node:
    {{"op":"add_node","id":"new_1","class_type":"UpscaleModelLoader",
      "inputs":{{"model_name":"RealESRGAN_x4plus.pth"}},"_meta":{{"title":"Upscaler"}}}},
    {{"op":"add_node","id":"new_2","class_type":"ImageUpscaleWithModel",
      "inputs":{{"upscale_model":["new_1",0],"image":["8",0]}}}},
    {{"op":"rewire","id":"9","input":"images","to":["new_2",0]}},
    // remove_node — delete and rewire consumers:
    {{"op":"remove_node","id":"10","rewire":{{"0":["4",0],"1":["4",1]}}}},
    // rewire — redirect a single input:
    {{"op":"rewire","id":"3","input":"model","to":["4",0]}}
  ],
  "inpaint_regions": [                                  // required if decision == inpaint
    {{
      "description":  "<region, e.g. 'left hand'>",
      "issue":        "<specific defect observed>",
      "correction":   "<what the inpaint should achieve>"
    }}
  ],
  "inpaint_prompt":  "<comma-separated tags — see PROMPT FORMAT>",  // required if decision == inpaint
  "inpaint_model":   "sdxl" | "flux" | "",             // optional model for inpaint pass
  "inpaint_loras":   []                                 // optional LoRA list for inpaint pass
}}
"""


def _build_model_section(cfg) -> str:  # type: ignore[no-untyped-def]
    """Build the model/LoRA availability section for the system prompt."""
    lines: list[str] = []

    models = cfg.models if hasattr(cfg, "models") else {}
    sdxl_ckpt = (models.get("sdxl") or {}).get("checkpoint", "")
    flux = models.get("flux") or {}
    flux_unet = flux.get("unet", "")

    available: list[str] = []
    if sdxl_ckpt:
        available.append(f'  sdxl  — SDXL checkpoint: {sdxl_ckpt}')
    if flux_unet:
        available.append(f'  flux  — Flux UNet: {flux_unet}')

    if available:
        lines.append("Available generative models:")
        lines.extend(available)
    else:
        lines.append("Available generative models: (none configured — model_type field will be ignored)")

    loras_cfg = models.get("loras") or {}
    has_loras = False
    for model_type in ("sdxl", "flux"):
        entries = loras_cfg.get(model_type) or []
        if not entries:
            continue
        has_loras = True
        lines.append(f"\nAvailable LoRAs for {model_type}:")
        for entry in entries:
            name     = entry.get("name", "")
            desc     = entry.get("description", "")
            strength = entry.get("default_strength", 1.0)
            trigger  = entry.get("trigger_word", "")
            trigger_str = f'  trigger_word: "{trigger}"' if trigger else "  no trigger word required"
            lines.append(f'  "{name}"')
            lines.append(f'    description:     {desc}')
            lines.append(f'    default_strength: {strength}')
            lines.append(f'    {trigger_str}')

    if not has_loras:
        lines.append("\nAvailable LoRAs: (none configured)")

    return "\n".join(lines)


def build_system_prompt(cfg=None) -> str:  # type: ignore[no-untyped-def]
    """Build the tactical LLM system prompt, injecting model/LoRA availability from config.

    Args:
        cfg: Config instance from config.load(). If None, uses a placeholder
             indicating no models are configured.

    Returns:
        Formatted system prompt string.
    """
    if cfg is None:
        model_section = "Available generative models: (config not provided)"
    else:
        model_section = _build_model_section(cfg)
    return _SYSTEM_PROMPT_BASE.format(model_section=model_section)


# Backward-compatible static export populated at import time from config.
# tactical_llm.py imports this name; it can also call build_system_prompt(cfg)
# directly for a freshly-built prompt.
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import load as _load_config
    SYSTEM_PROMPT = build_system_prompt(_load_config())
except Exception:
    SYSTEM_PROMPT = build_system_prompt(None)


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


def _format_workflow(workflow: dict) -> str:
    """Format a ComfyUI workflow as a compact node list for the decision prompt.

    Shows node IDs, class types, wire connections, and scalar parameters so
    the LLM can reason about graph topology when constructing graph_ops.
    """
    lines: list[str] = []
    for node_id, node in sorted(workflow.items(), key=lambda x: (len(x[0]), x[0])):
        ct    = node.get("class_type", "?")
        title = node.get("_meta", {}).get("title", "")
        label = f'  "{node_id}"  {ct}' + (f'  [{title}]' if title else "")
        inputs = node.get("inputs", {})
        wires  = {k: v for k, v in inputs.items() if isinstance(v, list)}
        params = {k: v for k, v in inputs.items() if not isinstance(v, list)}
        parts: list[str] = []
        if wires:
            parts.append("wires: " + ", ".join(f"{k}←{v}" for k, v in wires.items()))
        if params:
            parts.append("params: " + ", ".join(f"{k}={v!r}" for k, v in params.items()))
        lines.append(label + ("  (" + "; ".join(parts) + ")" if parts else ""))
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
    taste_context: str | None = None,
    workflow: dict | None = None,
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

    if taste_context:
        lines += [
            "── USER TASTE PROFILE (cross-session learned preferences) ───",
            taste_context,
            "",
        ]

    if workflow:
        lines += [
            "── WORKFLOW GRAPH (current node topology) ───────────────────",
            _format_workflow(workflow),
            "",
            "  To modify this graph on retry, emit retry_graph_ops — a list of",
            "  add_node / remove_node / rewire operations using the IDs above.",
            "  New nodes use placeholder IDs (e.g. \"new_1\") in their inputs;",
            "  placeholders are resolved to real IDs before submission.",
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
