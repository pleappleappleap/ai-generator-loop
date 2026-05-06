#!/usr/bin/env python3
"""Environment validation for the ai-image pipeline.

Checks Python version, installed packages, service availability, model
files, built Rust binaries, and virtual environments. Run via::

    make check

Exit codes:
    0   All required checks passed (warnings do not affect exit code)
    1   One or more required checks failed
"""

import importlib
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent
CONFIG_PATH = REPO_ROOT / "config.yaml"

_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

PASS  = f"{_GREEN}✓{_RESET}"
FAIL  = f"{_RED}✗{_RESET}"
WARN  = f"{_YELLOW}⚠{_RESET}"

_failures = 0


def check(label: str, ok: bool, detail: str = "", warn_only: bool = False) -> None:
    """Print a pass/fail/warn line and count failures."""
    global _failures
    icon = PASS if ok else (WARN if warn_only else FAIL)
    line = f"  {icon}  {label}"
    if detail:
        line += f"  [{detail}]"
    print(line)
    if not ok and not warn_only:
        _failures += 1


def _tcp_open(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _cmd_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _try_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def _try_import_in(python: Path, name: str) -> bool:
    """Check whether *name* is importable under a specific Python interpreter."""
    try:
        result = subprocess.run(
            [str(python), "-c", f"import {name}"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _resolve(value: str, default: Path) -> Path:
    """Return *default* when value is 'auto', otherwise expand the path."""
    if value == "auto":
        return default
    return Path(value).expanduser()


def main() -> None:
    print(f"\n{_BOLD}── ai-image Environment Check ──────────────────────────────────{_RESET}")

    # ── Config ────────────────────────────────────────────────────────
    print(f"\n{_BOLD}Configuration:{_RESET}")
    config_ok = CONFIG_PATH.exists()
    check("config.yaml exists",
          config_ok,
          detail="run: make config" if not config_ok else "")

    if not config_ok:
        print("\nCannot continue without config.yaml — run: make config\n")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    paths    = cfg.get("paths", {})
    models   = cfg.get("models", {})
    compute  = cfg.get("compute", {})
    broker   = cfg.get("broker", {})
    system   = cfg.get("system", {})
    tactical = cfg.get("tactical", {})

    # Resolve auto paths
    root_path    = _resolve(paths.get("root", "auto"),    REPO_ROOT)
    scorers_root = _resolve(paths.get("scorers", "auto"), REPO_ROOT / "loop" / "scorers")
    comfyui_root = _resolve(paths.get("comfyui", "auto"), REPO_ROOT / "loop" / "ComfyUI")
    lancedb_path = _resolve(paths.get("lancedb", "auto"), REPO_ROOT / "lancedb")

    # ── Python ────────────────────────────────────────────────────────
    print(f"\n{_BOLD}Python:{_RESET}")
    vi = sys.version_info
    check(f"Python >= 3.11  (found {vi.major}.{vi.minor}.{vi.micro})",
          vi >= (3, 11))

    for pkg, import_name in [
        ("pyyaml",          "yaml"),
        ("stomp.py",        "stomp"),
        ("torch",           "torch"),
        ("open_clip",       "open_clip"),
        ("Pillow",          "PIL"),
        ("lancedb",         "lancedb"),
        ("huggingface_hub", "huggingface_hub"),
    ]:
        check(f"import {pkg}", _try_import(import_name))

    # transformers and llama_cpp live in the scorers venv, not the root venv.
    scorers_python = scorers_root / "venv" / "bin" / "python3"
    for pkg, import_name in [
        ("transformers", "transformers"),
        ("llama_cpp",    "llama_cpp"),
    ]:
        check(f"import {pkg}  (scorers venv)",
              _try_import_in(scorers_python, import_name))

    # ── Compute backends ──────────────────────────────────────────────
    print(f"\n{_BOLD}Compute backends:{_RESET}")
    for component, cfg_key in [
        ("comfyui",        "comfyui"),
        ("clip_scorer",    "clip_scorer"),
        ("artifact_scorer","artifact_scorer"),
        ("vlm_scorer",     "vlm_scorer"),
        ("tactical_llm",   "tactical_llm"),
    ]:
        comp_cfg = compute.get(cfg_key, {})
        backend  = comp_cfg.get("backend", "auto")

        # Resolve auto
        resolved = backend
        if backend == "auto":
            if platform.system() == "Darwin":
                resolved = "mps"
            elif shutil.which("nvcc") or shutil.which("nvidia-smi"):
                resolved = "cuda"
            elif shutil.which("rocm-smi"):
                resolved = "rocm"
            else:
                resolved = "cpu"

        label = f"{component:<20} backend={backend}"
        if resolved != backend:
            label += f" → {resolved}"
        check(label, True)

    # Verify resolved backend is usable
    sample = compute.get("clip_scorer", {}).get("backend", "auto")
    if sample == "auto":
        sample = "mps" if platform.system() == "Darwin" else "cpu"
    if sample == "mps":
        try:
            import torch
            check("MPS available  (torch.backends.mps.is_available)",
                  torch.backends.mps.is_available())
        except (ImportError, AttributeError):
            check("MPS check", False, detail="torch not installed or MPS not supported on this platform")
    elif sample in ("cuda", "rocm"):
        try:
            import torch
            check(f"{sample.upper()} available  (torch.cuda.is_available)",
                  torch.cuda.is_available())
        except ImportError:
            check(f"{sample.upper()} check", False, detail="torch not installed")

    # ── Services ──────────────────────────────────────────────────────
    print(f"\n{_BOLD}Services:{_RESET}")
    check("Artemis AMQP reachable   (localhost:5672)",
          _tcp_open("127.0.0.1", 5672))
    check("Artemis STOMP reachable  (localhost:61613)",
          _tcp_open("127.0.0.1", 61613))

    artemis_console = broker.get("artemis_console", "http://localhost:8161")
    try:
        console_port = int(artemis_console.rstrip("/").split(":")[-1])
    except ValueError:
        console_port = 8161
    check(f"Artemis console reachable  ({artemis_console})",
          _tcp_open("127.0.0.1", console_port),
          warn_only=True)

    comfyui_url = broker.get("comfyui_url", "http://127.0.0.1:8188")
    try:
        comfyui_port = int(comfyui_url.rstrip("/").split(":")[-1])
    except ValueError:
        comfyui_port = 8188
    check(f"ComfyUI reachable  ({comfyui_url})",
          _tcp_open("127.0.0.1", comfyui_port),
          warn_only=True)

    # ── System binaries ───────────────────────────────────────────────
    print(f"\n{_BOLD}System binaries:{_RESET}")
    pkg_mgr = system.get("package_manager", "")
    prefix  = system.get("homebrew_prefix", "")
    if pkg_mgr == "homebrew" and prefix and prefix != "auto":
        os.environ["PATH"] = (
            f"{prefix}/bin:{prefix}/sbin:" + os.environ.get("PATH", "")
        )
    check("yq",    _cmd_exists("yq"))
    # cargo may be installed by rustup without being on the system PATH.
    cargo_ok = _cmd_exists("cargo") or (Path.home() / ".cargo" / "bin" / "cargo").exists()
    check("cargo", cargo_ok)
    check("java",  _cmd_exists("java"),
          detail="required by ActiveMQ Artemis")

    # ── Rust binaries ─────────────────────────────────────────────────
    print(f"\n{_BOLD}Rust binaries:{_RESET}")
    release_dir = scorers_root / "target" / "release"
    for name in ("router", "aggregator", "coordinator", "lancedb_manager"):
        bin_path = release_dir / name
        check(f"{name} binary", bin_path.exists(),
              detail=f"run: cd loop/scorers && cargo build --release" if not bin_path.exists() else str(bin_path))

    # ── Model files ───────────────────────────────────────────────────
    print(f"\n{_BOLD}Models:{_RESET}")

    vlm_cfg  = models.get("vlm", {})
    vlm_dir  = _resolve(vlm_cfg.get("dir", "auto"), scorers_root / "models" / "vlm")
    vlm_file = vlm_dir / vlm_cfg.get("filename", "")
    check(f"VLM model  ({vlm_cfg.get('filename', '?')})",
          vlm_file.exists(), detail=str(vlm_file))

    art_cfg = models.get("artifact_detector", {})
    art_dir = _resolve(art_cfg.get("dir", "auto"),
                       scorers_root / "models" / "artifact-detector")
    check("Artifact detector dir", art_dir.exists(), detail=str(art_dir))

    tac_model_cfg = tactical.get("model", {})
    tac_dir  = _resolve(tac_model_cfg.get("dir", "auto"),
                        scorers_root / "models" / "tactical")
    tac_file = tac_dir / tac_model_cfg.get("filename", "")
    check(f"Tactical LLM  ({tac_model_cfg.get('filename', '?')})",
          tac_file.exists(), detail=str(tac_file))

    # ── Virtual environments ──────────────────────────────────────────
    print(f"\n{_BOLD}Virtual environments:{_RESET}")
    check("Root venv",
          (REPO_ROOT / "venv").exists(),
          detail=str(REPO_ROOT / "venv"))
    check("Scorers venv",
          (scorers_root / "venv").exists(),
          detail=str(scorers_root / "venv"))
    check("ComfyUI venv",
          (comfyui_root / "venv").exists(),
          detail=str(comfyui_root / "venv"))

    # ── Storage ───────────────────────────────────────────────────────
    print(f"\n{_BOLD}Storage:{_RESET}")
    check("LanceDB directory exists", lancedb_path.exists(),
          detail=str(lancedb_path), warn_only=True)
    db_path_cfg = cfg.get("database", {}).get("path", "auto")
    db_path = _resolve(db_path_cfg, REPO_ROOT / "pipeline.db")
    check("SQLite database exists", db_path.exists(),
          detail=str(db_path), warn_only=True)

    # ── Summary ───────────────────────────────────────────────────────
    print()
    print("─" * 64)
    if _failures == 0:
        print(f"  {PASS}  {_BOLD}All required checks passed.{_RESET}")
    else:
        print(f"  {FAIL}  {_BOLD}{_failures} required check(s) failed.{_RESET}")
    print()
    sys.exit(0 if _failures == 0 else 1)


if __name__ == "__main__":
    main()
