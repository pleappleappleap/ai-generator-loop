"""Model download script for the AI image generation pipeline.

Downloads VLM scorer, tactical LLM, artifact detector, and any LoRAs
listed in config.yaml using the ``hf`` CLI (huggingface-cli).

No HuggingFace token is required for public repos.  For gated repos,
run ``hf login`` once to store credentials, or set HF_TOKEN in the
environment — the ``hf`` CLI picks it up automatically.

Civitai LoRAs still require CIVITAI_TOKEN:
  CIVITAI_TOKEN=... make models

Sources supported per model/LoRA entry in config.yaml:
  hf:<owner>/<repo>/<filename>  HuggingFace Hub single file
  hf:<owner>/<repo>             HuggingFace Hub repo snapshot
  civitai:<model_version_id>    Civitai model version
  local:<filename>              Already on disk — no download
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

_IS_MACOS = platform.system() == "Darwin"

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from config import load as _load_config  # noqa: E402

_cfg = _load_config()
_CIVITAI_TOKEN: str | None = os.environ.get("CIVITAI_TOKEN") or None


# ---------------------------------------------------------------------------
# HuggingFace helpers (hf CLI)
# ---------------------------------------------------------------------------

def _hf_bin() -> str:
    hf = shutil.which("hf")
    if not hf:
        raise RuntimeError(
            "hf not found in PATH — run 'make prereqs-system' to install huggingface-cli"
        )
    return hf


def _hf_download_file(repo_id: str, filename: str, local_dir: str) -> None:
    dest = Path(local_dir) / filename
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"    Already present ({dest.stat().st_size // 1_048_576} MB) — skipping.")
        return
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    print(f"    hf download {repo_id} {filename}")
    subprocess.run(
        [_hf_bin(), "download", repo_id, filename, "--local-dir", local_dir],
        check=True,
    )


def _hf_snapshot(repo_id: str, local_dir: str) -> None:
    dest = Path(local_dir)
    if dest.exists() and any(f for f in dest.iterdir() if not f.name.startswith(".")):
        print(f"    Already present — skipping.")
        return
    dest.mkdir(parents=True, exist_ok=True)
    print(f"    hf download {repo_id} (snapshot)")
    subprocess.run(
        [_hf_bin(), "download", repo_id, "--local-dir", local_dir],
        check=True,
    )


# ---------------------------------------------------------------------------
# Civitai helpers
# ---------------------------------------------------------------------------

def _civitai_download(version_id: str | int, dest_path: str) -> None:
    dest = Path(dest_path)
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"    Already present ({dest.stat().st_size // 1_048_576} MB) — skipping.")
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
    if source.startswith("hf:"):
        rest = source[3:]
        parts = rest.split("/")
        if len(parts) >= 3:
            # hf:owner/repo/filename  →  single-file download
            repo_id = "/".join(parts[:-1])
            filename = parts[-1]
            _hf_download_file(repo_id, filename, dest_dir)
        else:
            # hf:owner/repo  →  snapshot (model names can contain dots)
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
    local_dir = vlm_cfg.get("dir", "")
    Path(local_dir).mkdir(parents=True, exist_ok=True)

    if _IS_MACOS:
        mlx_name = vlm_cfg.get("mlx_model_name", "")
        mlx_source = vlm_cfg.get("mlx_source", "")
        if not mlx_name or not mlx_source:
            print("==> VLM scorer (macOS/mlx): mlx_model_name/mlx_source not configured — skipping.")
            print("    Set vlm.mlx_model_name and vlm.mlx_source in config.yaml.")
            return
        print(f"==> VLM scorer (macOS/mlx): {mlx_name}")
        _resolve_source(mlx_source, str(Path(local_dir) / mlx_name), mlx_name)
    else:
        filename = vlm_cfg.get("filename", "")
        source = vlm_cfg.get("source", "")
        mmproj_filename = vlm_cfg.get("mmproj_filename", "")
        mmproj_source = vlm_cfg.get("mmproj_source", "")
        if not filename or not source:
            print("==> VLM scorer (Linux/llama.cpp): filename/source not configured — skipping.")
            return
        print(f"==> VLM scorer (Linux/llama.cpp): {filename}")
        _resolve_source(source, local_dir, filename)
        if mmproj_filename and mmproj_source:
            print(f"==> VLM mmproj: {mmproj_filename}")
            _resolve_source(mmproj_source, local_dir, mmproj_filename)


def _download_tactical() -> None:
    tac = _cfg.tactical.get("model", {})
    local_dir = tac.get("dir", "")
    if _IS_MACOS:
        name = tac.get("name", "")
        source = tac.get("source", "")
        if not name or not source:
            print("==> Tactical LLM (macOS/mlx): name/source not configured — skipping.")
            return
        print(f"==> Tactical LLM (macOS/mlx): {name}")
        _resolve_source(source, str(Path(local_dir) / name), name)
    else:
        linux = tac.get("linux", {})
        name = linux.get("name", "")
        source = linux.get("source", "")
        if not name or not source:
            print("==> Tactical LLM (Linux/llama.cpp): tactical.model.linux.name/source not configured — skipping.")
            return
        print(f"==> Tactical LLM (Linux/llama.cpp): {name}")
        _resolve_source(source, local_dir, name)


def _download_strategic() -> None:
    strat = _cfg.strategic.get("model", {})
    local_dir = strat.get("dir", "")
    if _IS_MACOS:
        name = strat.get("name", "")
        source = strat.get("source", "")
        if not name or not source:
            print("==> Strategic LLM (macOS/mlx): name/source not configured — skipping.")
            return
        print(f"==> Strategic LLM (macOS/mlx): {name}")
        _resolve_source(source, str(Path(local_dir) / name), name)
    else:
        linux = strat.get("linux", {})
        name = linux.get("name", "")
        source = linux.get("source", "")
        if not name or not source:
            print("==> Strategic LLM (Linux/llama.cpp): strategic.model.linux.name/source not configured — skipping.")
            return
        print(f"==> Strategic LLM (Linux/llama.cpp): {name}")
        _resolve_source(source, local_dir, name)


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
    print(f"Civitai: {'authenticated' if _CIVITAI_TOKEN else 'no token — Civitai sources will fail'}")
    print()

    _download_artifact_detector()
    _download_vlm()
    _download_tactical()
    _download_strategic()
    _download_loras()

    ckpt_dir = Path(_cfg.paths.get("comfyui", "")) / "models" / "checkpoints"
    print()
    print("==> Models done.")
    print(f"    SDXL/Flux checkpoints → {ckpt_dir}/")


if __name__ == "__main__":
    main()
