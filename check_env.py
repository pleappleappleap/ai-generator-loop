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
    return subprocess.run(["which", cmd], capture_output=True).returncode == 0


def _try_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


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

    paths     = cfg.get("paths", {})
    models    = cfg.get("models", {})
    gpu_cfg   = cfg.get("gpu", {})
    broker    = cfg.get("broker", {})
    system    = cfg.get("system", {})

    # ── Python ────────────────────────────────────────────────────────
    print(f"\n{_BOLD}Python:{_RESET}")
    vi = sys.version_info
    check(f"Python >= 3.11  (found {vi.major}.{vi.minor}.{vi.micro})",
          vi >= (3, 11))

    for pkg, import_name in [
        ("pyyaml",        "yaml"),
        ("pika",          "pika"),
        ("torch",         "torch"),
        ("open_clip",     "open_clip"),
        ("Pillow",        "PIL"),
        ("transformers",  "transformers"),
        ("lancedb",       "lancedb"),
        ("redis",         "redis"),
        ("llama_cpp",     "llama_cpp"),
    ]:
        check(f"import {pkg}", _try_import(import_name))

    # ── GPU ───────────────────────────────────────────────────────────
    print(f"\n{_BOLD}GPU:{_RESET}")
    backend = gpu_cfg.get("backend", "unknown")
    check(f"Configured backend: {backend}", True)

    if backend == "mps":
        try:
            import torch
            avail = torch.backends.mps.is_available()
            check("MPS available (torch.backends.mps.is_available)", avail)
        except ImportError:
            check("MPS check", False, detail="torch not installed")
    elif backend in ("cuda", "rocm"):
        try:
            import torch
            avail = torch.cuda.is_available()
            check(f"{backend.upper()} available (torch.cuda.is_available)", avail)
        except ImportError:
            check(f"{backend.upper()} check", False, detail="torch not installed")
    else:
        check("CPU mode — no GPU check needed", True)

    # ── Services ──────────────────────────────────────────────────────
    print(f"\n{_BOLD}Services:{_RESET}")
    check("RabbitMQ reachable  (localhost:5672)",
          _tcp_open("127.0.0.1", 5672))
    check("RabbitMQ mgmt UI  (localhost:15672)",
          _tcp_open("127.0.0.1", 15672),
          warn_only=True)
    check("Redis reachable  (localhost:6379)",
          _tcp_open("127.0.0.1", 6379))

    comfyui_url = broker.get("comfyui_url", "http://127.0.0.1:8188")
    try:
        port = int(comfyui_url.rstrip("/").split(":")[-1])
    except ValueError:
        port = 8188
    check(f"ComfyUI reachable  ({comfyui_url})",
          _tcp_open("127.0.0.1", port),
          warn_only=True)

    # ── System binaries ───────────────────────────────────────────────
    print(f"\n{_BOLD}System binaries:{_RESET}")
    pkg_mgr = system.get("package_manager", "")
    prefix  = system.get("homebrew_prefix", "")
    if pkg_mgr == "homebrew" and prefix:
        os.environ["PATH"] = (
            f"{prefix}/bin:{prefix}/sbin:" + os.environ.get("PATH", "")
        )
    check("rabbitmq-server", _cmd_exists("rabbitmq-server"))
    check("redis-server",    _cmd_exists("redis-server"))
    check("yq",              _cmd_exists("yq"))

    # ── Rust binaries ─────────────────────────────────────────────────
    print(f"\n{_BOLD}Rust binaries:{_RESET}")
    scorers_root = Path(paths.get("scorers", "")).expanduser()
    router_bin   = scorers_root / "router"      / "target" / "release" / "router"
    agg_bin      = scorers_root / "aggregator"  / "target" / "release" / "aggregator"
    check("router binary",     router_bin.exists(),  detail=str(router_bin))
    check("aggregator binary", agg_bin.exists(),     detail=str(agg_bin))

    # ── Model files ───────────────────────────────────────────────────
    print(f"\n{_BOLD}Models:{_RESET}")
    vlm = models.get("vlm", {})
    vlm_dir  = Path(vlm.get("dir", "")).expanduser()
    vlm_file = vlm_dir / vlm.get("filename", "")
    check(f"VLM model  ({vlm.get('filename', '')})",
          vlm_file.exists(), detail=str(vlm_file))

    art_dir = Path(models.get("artifact_detector", {}).get("dir", "")).expanduser()
    check("Artifact detector model dir",
          art_dir.exists(), detail=str(art_dir))

    # ── Virtual environments ──────────────────────────────────────────
    print(f"\n{_BOLD}Virtual environments:{_RESET}")
    comfyui_root = Path(paths.get("comfyui", "")).expanduser()
    check("ComfyUI venv",
          (comfyui_root / "venv").exists(),
          detail=str(comfyui_root / "venv"))
    check("Scorers venv",
          (scorers_root / "venv").exists(),
          detail=str(scorers_root / "venv"))

    # ── LanceDB store ─────────────────────────────────────────────────
    print(f"\n{_BOLD}Storage:{_RESET}")
    lancedb_path = Path(paths.get("lancedb", "")).expanduser()
    check("LanceDB directory exists",
          lancedb_path.exists(),
          detail=str(lancedb_path),
          warn_only=True)

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
