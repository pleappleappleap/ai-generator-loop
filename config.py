"""AI image pipeline configuration loader.

Reads config.yaml from the repository root (the directory containing this
file). Expands ~ and environment variables in all string values. Provides
typed property access to all configuration sections.

Run ``make config-default`` to copy the defaults into config.yaml, then
optionally ``make menuconfig`` to customise values interactively.

Example::

    from config import load, resolve_backend
    cfg = load()
    backend = resolve_backend(cfg.compute["clip_scorer"]["backend"])
    print(cfg.paths["scorers"])
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


def _resolve_auto_paths(data: dict) -> dict:
    """Resolve ``"auto"`` sentinel values in well-known path fields.

    The config.yaml uses ``"auto"`` as a placeholder for paths that are
    derived from the repository root at runtime.  This mirrors the shell
    logic in ``loop.sh`` so that Python processes see fully-resolved
    absolute paths rather than the literal string ``"auto"``.
    """
    root = str(_REPO_ROOT)

    paths = data.get("paths", {})
    if paths.get("root") == "auto":
        paths["root"] = root
    r = paths.get("root", root)

    if paths.get("scorers") == "auto":
        paths["scorers"] = str(Path(r) / "loop" / "scorers")
    if paths.get("comfyui") == "auto":
        paths["comfyui"] = str(Path(r) / "loop" / "ComfyUI")
    if paths.get("output") == "auto":
        paths["output"] = str(Path(r) / "loop" / "output")

    scorers = paths.get("scorers", str(Path(r) / "loop" / "scorers"))

    models = data.get("models", {})
    vlm = models.get("vlm", {})
    if vlm.get("dir") == "auto":
        vlm["dir"] = str(Path(scorers) / "models" / "vlm")
    artifact = models.get("artifact_detector", {})
    if artifact.get("dir") == "auto":
        artifact["dir"] = str(Path(scorers) / "models" / "artifact-detector")

    tactical = data.get("tactical", {})
    tmodel = tactical.get("model", {})
    if tmodel.get("dir") == "auto":
        tmodel["dir"] = str(Path(scorers) / "models" / "tactical")

    strategic = data.get("strategic", {})
    smodel = strategic.get("model", {})
    if smodel.get("dir") == "auto":
        smodel["dir"] = str(Path(r) / "strategic-llm" / "models")

    return data


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
        compute: Per-component compute target configuration.
        thresholds: Scorer acceptance thresholds.
        broker: Artemis and ComfyUI connection parameters.
        database: PostgreSQL connection parameters.
        system: Host system parameters (package manager, Homebrew prefix).
    """

    def __init__(self, data: dict) -> None:
        self._data: dict = _expand_recursive(_resolve_auto_paths(data))

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
        """PostgreSQL connection parameters."""
        return self._data.get("database", {})

    @property
    def system(self) -> dict:
        """Host system configuration."""
        return self._data["system"]

    @property
    def tactical(self) -> dict:
        """Tactical LLM configuration."""
        return self._data.get("tactical", {})

    @property
    def strategic(self) -> dict:
        """Strategic LLM configuration."""
        return self._data.get("strategic", {})


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
            Run ``make config-default`` to create it.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {_CONFIG_PATH}\n"
            "Run: make config-default"
        )
    with open(_CONFIG_PATH) as f:
        data = yaml.safe_load(f)
    return Config(data)
