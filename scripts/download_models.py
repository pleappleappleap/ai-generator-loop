"""Model download script for the AI image generation pipeline.

Downloads VLM scorer, tactical LLM, artifact detector, and any LoRAs
listed in config.yaml.

Authentication is read from environment variables only — credentials are
never stored in config files:

  HF_TOKEN      HuggingFace token (required for gated repos such as the
                bartowski GGUF repacks).  Create at:
                https://huggingface.co/settings/tokens

  CIVITAI_TOKEN Civitai API token (required for LoRAs sourced from Civitai).
                Create at: https://civitai.com/user/account

Pass tokens for a single invocation:
  HF_TOKEN=hf_xxx CIVITAI_TOKEN=civ_xxx make models

Sources supported per model/LoRA entry in config.yaml:
  hf:<repo_id>/<filename>      HuggingFace Hub single file
  hf:<repo_id>                 HuggingFace Hub repo snapshot
  civitai:<model_version_id>   Civitai model version
  local:<filename>             Already on disk — no download
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from config import load as _load_config  # noqa: E402

_cfg = _load_config()
_HF_TOKEN: str | None = os.environ.get("HF_TOKEN") or None
_CIVITAI_TOKEN: str | None = os.environ.get("CIVITAI_TOKEN") or None


# ---------------------------------------------------------------------------
# HuggingFace helpers
# ---------------------------------------------------------------------------

def _hf_download_file(repo_id: str, filename: str, local_dir: str) -> str:
    """Download a single file from a HuggingFace repo."""
    from huggingface_hub import hf_hub_download
    dest = Path(local_dir) / filename
    size = dest.stat().st_size if dest.exists() else 0
    if size > 1_000_000:
        print(f"    Already present ({size // 1_048_576} MB) — skipping.")
        return str(dest)
    if not _HF_TOKEN:
        print(f"    ERROR: HF_TOKEN not set — cannot download {repo_id}/{filename}.")
        print("    Run:  HF_TOKEN=hf_xxx make models")
        sys.exit(1)
    print(f"    Downloading {repo_id}/{filename} …")
    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=local_dir,
        token=_HF_TOKEN,
        force_download=bool(size),  # force re-download of corrupt stubs
    )


def _hf_snapshot(repo_id: str, local_dir: str) -> str:
    """Download an entire HuggingFace repo snapshot."""
    from huggingface_hub import snapshot_download
    print(f"    Syncing {repo_id} …")
    return snapshot_download(repo_id=repo_id, local_dir=local_dir, token=_HF_TOKEN)


# ---------------------------------------------------------------------------
# Civitai helpers
# ---------------------------------------------------------------------------

def _civitai_download(version_id: str | int, dest_path: str) -> None:
    """Download a Civitai model version to dest_path."""
    import urllib.request

    dest = Path(dest_path)
    size = dest.stat().st_size if dest.exists() else 0
    if size > 1_000_000:
        print(f"    Already present ({size // 1_048_576} MB) — skipping.")
        return
    if not _CIVITAI_TOKEN:
        print(f"    ERROR: CIVITAI_TOKEN not set — cannot download civitai:{version_id}.")
        print("    Run:  CIVITAI_TOKEN=... make models")
        sys.exit(1)

    url = f"https://civitai.com/api/download/models/{version_id}?token={_CIVITAI_TOKEN}"
    print(f"    Downloading civitai:{version_id} → {dest.name} …")
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _reporthook(block_num: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            pct = min(100, block_num * block_size * 100 // total_size)
            print(f"\r    {pct}%", end="", flush=True)

    urllib.request.urlretrieve(url, str(dest), reporthook=_reporthook)
    print()


# ---------------------------------------------------------------------------
# Dispatch by source spec
# ---------------------------------------------------------------------------

def _resolve_source(source: str, dest_dir: str, dest_filename: str) -> None:
    """Download a model file according to its source URI."""
    if source.startswith("hf:"):
        rest = source[3:]
        parts = rest.split("/")
        if len(parts) >= 2 and "." in parts[-1]:
            repo_id = "/".join(parts[:-1])
            filename = parts[-1]
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            _hf_download_file(repo_id, filename, dest_dir)
        else:
            _hf_snapshot(rest, dest_dir)

    elif source.startswith("civitai:"):
        version_id = source[8:]
        _civitai_download(version_id, str(Path(dest_dir) / dest_filename))

    elif source.startswith("local:"):
        filename = source[6:]
        path = Path(dest_dir) / filename
        if not path.exists():
            print(f"    WARNING: local file not found: {path}")
        else:
            print(f"    Local — {filename} found.")

    else:
        print(f"    WARNING: unknown source scheme '{source}' — skipping.")


# ---------------------------------------------------------------------------
# Per-model download routines
# ---------------------------------------------------------------------------

def _download_artifact_detector() -> None:
    cfg = _cfg.models.get("artifact_detector", {})
    local_dir = cfg.get("dir", "")
    source = cfg.get("source", "hf:umm-maybe/AI-image-detector")
    print(f"==> Artifact detector ({source})")
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    _resolve_source(source, local_dir, "")


def _download_vlm() -> None:
    vlm_cfg = _cfg.models.get("vlm", {})
    filename = vlm_cfg.get("filename", "")
    local_dir = vlm_cfg.get("dir", "")
    source = vlm_cfg.get("source", "")
    if not filename or not local_dir or not source:
        print("==> VLM scorer: no filename/source configured — skipping.")
        return
    print(f"==> VLM scorer: {filename}")
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    _resolve_source(source, local_dir, filename)


def _download_tactical() -> None:
    tac = _cfg.tactical.get("model", {})
    filename = tac.get("filename", "")
    local_dir = tac.get("dir", "")
    source = tac.get("source", "")
    if not filename or not local_dir or not source:
        print("==> Tactical LLM: no filename/source configured — skipping.")
        return
    print(f"==> Tactical LLM: {filename}")
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    _resolve_source(source, local_dir, filename)


def _download_loras() -> None:
    loras_cfg = _cfg.models.get("loras", {})
    comfyui_root = Path(_cfg.paths.get("comfyui", ""))
    lora_dir = comfyui_root / "models" / "loras"

    for model_type in ("sdxl", "flux"):
        entries = loras_cfg.get(model_type) or []
        for entry in entries:
            name = entry.get("name", entry.get("filename", "?"))
            filename = entry.get("filename", "")
            source = entry.get("source", "")
            print(f"==> LoRA [{model_type}] {name}: {filename}")
            if not source:
                print("    No source — skipping.")
                continue
            lora_dir.mkdir(parents=True, exist_ok=True)
            _resolve_source(source, str(lora_dir), filename)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"HuggingFace: {'authenticated' if _HF_TOKEN else 'no token — gated repos will fail'}")
    print(f"Civitai:     {'authenticated' if _CIVITAI_TOKEN else 'no token — Civitai sources will fail'}")
    print()

    _download_artifact_detector()
    _download_vlm()
    _download_tactical()
    _download_loras()

    ckpt_dir = Path(_cfg.paths.get("comfyui", "")) / "models" / "checkpoints"
    print()
    print("==> Models done.")
    print(f"    SDXL/Flux checkpoints → {ckpt_dir}/")


if __name__ == "__main__":
    main()
