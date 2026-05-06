"""LanceDB retrieval helpers for tactical LLM context.

Provides two retrieval functions used by the tactical LLM before each
decision:

  session_history:  All prior Loop records for the current session_uuid,
                    ordered by sequence_number. Gives the LLM visibility
                    into what has already been tried in this session and
                    how the scores have trended.

  similar_past:     Top-K Loop records from *other* sessions whose prompt
                    embedding is most similar to the current prompt embedding.
                    Gives the LLM examples of what worked (or didn't) on
                    semantically similar prompts in the past.

Both functions add a computed ``vlm_mean`` float to each record for
convenience. The VLM mean is the arithmetic mean of the five VLM dimension
scores.

All functions open and close LanceDB connections per call. The tactical LLM
process is I/O-bound between decisions, so connection overhead is negligible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import lancedb
import open_clip
import torch

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import load as _load_config  # noqa: E402

_cfg = _load_config()

# CLIP model for embedding the current prompt (same model as scorers use)
_clip_cfg = _cfg.models["clip"]
_model, _, _ = open_clip.create_model_and_transforms(
    _clip_cfg["name"], pretrained=_clip_cfg["pretrained"]
)
_tokenizer = open_clip.get_tokenizer(_clip_cfg["name"])

_backend = _cfg.compute["clip_scorer"]["backend"]
if _backend == "mps":
    _device = torch.device("mps")
elif _backend in ("cuda", "rocm"):
    _device = torch.device("cuda")
else:
    _device = torch.device("cpu")

_model = _model.to(_device)


def _get_db() -> lancedb.LanceDBConnection:
    """Open and return a LanceDB connection to the configured store."""
    return lancedb.connect(_cfg.paths["lancedb"])


def _add_vlm_mean(record: dict) -> dict:
    """Add a ``vlm_mean`` key (float) to a Loop record dict.

    Computes the arithmetic mean of the five VLM dimension scores. If any
    dimension is missing or None, it is excluded from the mean. Returns the
    record unmodified if no VLM dimensions are present.

    Args:
        record: A flat dict matching the Loop schema.

    Returns:
        The input dict with a ``vlm_mean`` key added.
    """
    dims = [
        record.get("vlm_photorealism"),
        record.get("vlm_anatomical"),
        record.get("vlm_interaction"),
        record.get("vlm_lighting"),
        record.get("vlm_prompt"),
    ]
    valid = [d for d in dims if d is not None]
    record["vlm_mean"] = sum(valid) / len(valid) if valid else None
    return record


def embed_prompt(prompt: str) -> list[float]:
    """Return the 512-dimensional CLIP text embedding for a prompt string.

    Uses the same CLIP ViT-L-14 model and tokeniser as the clip_scorer.
    The returned vector is L2-normalised to unit length, consistent with
    the embeddings stored in LanceDB.

    Args:
        prompt: Natural language prompt string to embed.

    Returns:
        List of 512 floats representing the normalised CLIP text embedding.
    """
    tokens = _tokenizer([prompt]).to(_device)
    with torch.no_grad():
        features = _model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze().tolist()


def session_history(
    session_uuid: str,
    limit: int | None = None,
) -> list[dict]:
    """Return prior Loop records for a session, ordered by sequence_number.

    Only records with sequence_number less than the current image (i.e.
    records already committed to LanceDB before this decision) are
    meaningful. The LanceDB manager writes records asynchronously, so very
    recent images may not yet appear.

    Args:
        session_uuid: The session UUID to retrieve history for.
        limit: If provided, return only the most recent ``limit`` records
            ordered by sequence_number descending.

    Returns:
        List of Loop record dicts, each augmented with a ``vlm_mean``
        float. Empty list if no records exist for this session.
    """
    cfg_limit = _cfg.tactical.get("retrieval", {}).get("session_history_limit", 10)
    effective_limit = limit if limit is not None else cfg_limit

    try:
        db = _get_db()
        if "loop" not in db.list_tables():
            return []
        table = db.open_table("loop")
        records = (
            table.search()
            .where(f"session_uuid = '{session_uuid}'")
            .to_list()
        )
        if not records:
            return []
        records = sorted(records, key=lambda r: r.get("sequence_number", 0))
        if effective_limit:
            records = records[-effective_limit:]
        return [_add_vlm_mean(r) for r in records]
    except Exception:
        return []


def similar_past(
    prompt_embedding: list[float],
    current_session_uuid: str,
    k: int | None = None,
) -> list[dict]:
    """Return Loop records from other sessions similar to the given prompt.

    Performs an approximate nearest-neighbour search on the
    ``prompt_embedding`` column of the Loop table, excluding records from
    ``current_session_uuid`` to avoid leaking the current session into its
    own context.

    Only accepted records (verdict == "candidate") are considered. This
    means the LLM sees examples of prompts that produced successful images,
    not a mixture of failures and successes that could confuse its
    calibration.

    Args:
        prompt_embedding: 512-dimensional normalised CLIP text embedding of
            the current prompt, as returned by :func:`embed_prompt`.
        current_session_uuid: Session UUID to exclude from results.
        k: Number of similar records to return. Defaults to
            ``tactical.retrieval.similar_sessions_k`` from config.

    Returns:
        List of up to ``k`` Loop record dicts from other sessions, each
        augmented with a ``vlm_mean`` float. Empty list if LanceDB is
        unavailable or empty.
    """
    cfg_k = _cfg.tactical.get("retrieval", {}).get("similar_sessions_k", 5)
    effective_k = k if k is not None else cfg_k

    try:
        db = _get_db()
        if "loop" not in db.list_tables():
            return []
        table = db.open_table("loop")
        records = (
            table.search(prompt_embedding)
            .where(
                f"session_uuid != '{current_session_uuid}'"
                " AND verdict = 'candidate'"
            )
            .limit(effective_k)
            .to_list()
        )
        if not records:
            return []
        return [_add_vlm_mean(r) for r in records]
    except Exception:
        return []
