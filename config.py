"""AI image pipeline configuration loader.

Reads config.yaml from the repository root (the directory containing this
file). Expands ~ and environment variables in all string values. Provides
typed property access to all configuration sections.

Run ``make config`` to generate config.yaml from the interactive TUI.

Example::

    from config import load, resolve_backend
    cfg = load()
    backend = resolve_backend(cfg.compute["clip_scorer"]["backend"])
    print(cfg.paths["lancedb"])
"""

from __future__ import annotations

import os
import platform
import shutil
from functools import cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"


def _expand(value: Any) -> Any:
    """Expand ``~`` and environment variables in string values."""
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    return value


def _expand_recursive(obj: Any) -> Any:
    """Recursively expand path strings throughout a config dict."""
    if isinstance(obj, dict):
        return {k: _expand_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_recursive(v) for v in obj]
    return _expand(obj)


def resolve_backend(backend: str) -> str:
    """Resolve ``"auto"`` to a concrete backend string for the current host.

    Detection order for ``"auto"``:

    1. macOS → ``"mps"`` (Metal Performance Shaders)
    2. NVIDIA CUDA present (``nvcc`` or ``nvidia-smi`` on PATH) → ``"cuda"``
    3. AMD ROCm present (``rocm-smi`` on PATH) → ``"rocm"``
    4. Fallback → ``"cpu"``

    Non-``"auto"`` values are returned unchanged, allowing explicit
    per-component overrides in ``config.yaml``.

    Args:
        backend: Value from ``compute.<component>.backend`` in config.

    Returns:
        One of ``"mps"``, ``"cuda"``, ``"rocm"``, or ``"cpu"``.
    """
    if backend != "auto":
        return backend
    if platform.system() == "Darwin":
        return "mps"
    if shutil.which("nvcc") or shutil.which("nvidia-smi"):
        return "cuda"
    if shutil.which("rocm-smi"):
        return "rocm"
    return "cpu"


class Config:
    """Typed accessor for pipeline configuration.

    All path string values are expanded (``~`` and ``$VAR``) on load.
    Access sections as plain dicts via the properties below.

    Attributes:
        paths: Repository and output directory paths.
        models: Model file locations and inference parameters.
        compute: Per-component compute target configuration (backend,
            n_gpu_layers).
        thresholds: Scorer acceptance thresholds.
        broker: Artemis and ComfyUI connection parameters.
        database: SQLite operational state store configuration.
        system: Host system parameters (package manager, Homebrew prefix).
    """

    def __init__(self, data: dict) -> None:
        self._data: dict = _expand_recursive(data)

    @property
    def paths(self) -> dict[str, str]:
        """Repository and output directory paths."""
        return self._data["paths"]

    @property
    def models(self) -> dict:
        """Model file locations and inference parameters."""
        return self._data["models"]

    @property
    def compute(self) -> dict:
        """Per-component compute target configuration."""
        return self._data["compute"]

    @property
    def thresholds(self) -> dict:
        """Scorer acceptance threshold values."""
        return self._data["thresholds"]

    @property
    def broker(self) -> dict:
        """Message broker and service connection parameters."""
        return self._data["broker"]

    @property
    def database(self) -> dict:
        """SQLite operational state store configuration."""
        return self._data.get("database", {})

    @property
    def system(self) -> dict:
        """Host system configuration."""
        return self._data["system"]

    @property
    def tactical(self) -> dict:
        """Tactical LLM configuration."""
        return self._data.get("tactical", {})

    def get(self, *keys: str, default: Any = None) -> Any:
        """Return a nested config value by key path.

        Args:
            *keys: Key path components, e.g. ``get("gpu", "backend")``.
            default: Value to return if the key path is absent.

        Returns:
            Config value at the given key path, or *default*.
        """
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


@cache
def load() -> Config:
    """Load and return the singleton Config instance.

    Reads ``config.yaml`` from the repository root. Result is cached;
    subsequent calls return the same object without re-reading disk.

    Returns:
        Loaded and path-expanded :class:`Config` instance.

    Raises:
        FileNotFoundError: If ``config.yaml`` does not exist.
            Run ``make config`` to generate it.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {_CONFIG_PATH}\n"
            "Run: make config"
        )
    with open(_CONFIG_PATH) as f:
        data = yaml.safe_load(f)
    return Config(data)
