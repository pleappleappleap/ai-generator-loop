"""LanceDB schema definitions for the AI image generation pipeline.

This module defines the Pydantic LanceModel classes that describe the two
persistent tables used by the pipeline:

  sessions: One record per user-initiated generation session.
  loop:     One record per generated image, whether accepted or rejected.

These models serve as the single source of truth for the schema. Both
lancedb_manager.py and the strategic LLM import from this module rather
than defining schema independently.

The vector embedding dimension is defined as a module-level constant
CLIP_EMBEDDING_DIM = 512, matching the ViT-L-14 CLIP model used throughout
the pipeline. Changing the CLIP model requires updating this constant and
migrating the LanceDB tables.
"""

from typing import Optional

from lancedb.pydantic import LanceModel, Vector


CLIP_EMBEDDING_DIM = 512
"""Dimensionality of CLIP ViT-L-14 embeddings.

Both prompt embeddings and image embeddings use this dimension. If the
CLIP model is changed to one with different embedding dimensionality,
this constant must be updated and the LanceDB tables must be migrated.
"""


class Session(LanceModel):
    """A single user-initiated generation session.

    Created when a user submits a prompt to the loop. One session
    contains many loop records, each representing one generated image.
    The sequence_number field on Loop records tracks position within
    the session for trend analysis.

    Attributes:
        session_uuid: Primary key. UUID string identifying this session.
        created_at: ISO 8601 timestamp of session creation.
        user_prompt: The original natural language prompt submitted by
            the user. May differ from the prompts used for individual
            generation attempts, which the tactical LLM may modify.
        prompt_embedding: 512-dimensional normalized CLIP text embedding
            of user_prompt. Used for session-level similarity search —
            finding previous sessions with similar user intent.
    """

    session_uuid: str
    created_at: str
    user_prompt: str
    prompt_embedding: Vector(CLIP_EMBEDDING_DIM)


class Loop(LanceModel):
    """A single generated image, accepted or rejected.

    One record is written per image that passes through the pipeline,
    regardless of verdict. Rejected images are stored without an
    image_path but with full scorer results and embeddings, enabling
    the strategic LLM to learn from failures as well as successes.

    The sequence_number field tracks position within the parent session,
    enabling the strategic LLM to analyze score trends across a session
    and determine whether the tactical LLM is converging toward or
    diverging from quality targets.

    workflow_params stores the complete generation parameter set as a
    JSON string. This includes checkpoint filename, sampler, CFG scale,
    step count, seed, ControlNet weights and modes, LoRA weights and
    strengths, and regional prompt assignments. Storing the full parameter
    set enables retrieval and replay of successful configurations.

    Attributes:
        image_uuid: Primary key. UUID string identifying this image.
        session_uuid: Foreign key to the sessions table.
        sequence_number: 1-indexed position of this image within the
            parent session. Used for trend analysis across a session.
        created_at: ISO 8601 timestamp of image generation.
        prompt: The actual prompt used for this generation attempt.
            May differ from session.user_prompt if the tactical LLM
            has modified the prompt across iterations.
        prompt_embedding: 512-dimensional normalized CLIP text embedding
            of prompt. Used for prompt-based similarity search.
        image_embedding: 512-dimensional normalized CLIP image embedding.
            Stored for all images including rejected candidates. Used for
            visual similarity search — finding generations that produced
            visually similar results regardless of the prompt used.
        workflow_path: Absolute path to the ComfyUI API-format workflow
            JSON file used for this generation.
        workflow_params: JSON-serialized dict of generation parameters.
            Includes checkpoint, sampler, cfg, steps, seed, and all
            ControlNet and LoRA configuration.
        clip_score: CLIP cosine similarity between image_embedding and
            prompt_embedding. Range 0.0–1.0. Higher is better.
        artifact_score: AI artifact detector confidence that the image
            is AI-generated. Range 0.0–1.0. Lower is better.
        vlm_photorealism: VLM assessment of photorealism. Range 0–10.
        vlm_anatomical: VLM assessment of anatomical coherence. Range 0–10.
        vlm_interaction: VLM assessment of character interaction
            plausibility. Range 0–10.
        vlm_lighting: VLM assessment of lighting consistency. Range 0–10.
        vlm_prompt: VLM assessment of prompt adherence. Range 0–10.
        vlm_issues: JSON-serialized list of specific issues observed by
            the VLM, e.g. ["extra fingers on left hand", "mismatched shadows"].
        vlm_recommendations: JSON-serialized list of specific prompt or
            parameter adjustments recommended by the VLM.
        verdict: "candidate" if the image passed all thresholds and was
            accepted by the tactical LLM, "rejected" otherwise.
        rejection_reason: "clip_threshold" if rejected by CLIP score,
            "artifact_threshold" if rejected by artifact confidence,
            None for accepted candidates.
        image_path: Absolute filesystem path to the saved image file.
            None for rejected images — embeddings and scores are stored
            but the image file is not retained.
    """

    # Identity
    image_uuid: str
    session_uuid: str
    sequence_number: int
    created_at: str

    # Prompt
    prompt: str
    prompt_embedding: Vector(CLIP_EMBEDDING_DIM)

    # Visual embedding — stored for all images including rejected
    image_embedding: Vector(CLIP_EMBEDDING_DIM)

    # Generation parameters
    workflow_path: str
    workflow_params: str  # JSON-serialized dict

    # Scorer results
    clip_score: float
    artifact_score: float
    vlm_photorealism: float
    vlm_anatomical: float
    vlm_interaction: float
    vlm_lighting: float
    vlm_prompt: float
    vlm_issues: str           # JSON-serialized list
    vlm_recommendations: str  # JSON-serialized list

    # Verdict
    verdict: str
    rejection_reason: Optional[str] = None
    image_path: Optional[str] = None
