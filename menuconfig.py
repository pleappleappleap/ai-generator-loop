#!/usr/bin/env python3
"""menuconfig-style TUI for ai-image pipeline configuration.

Presents a Linux kernel menuconfig-inspired interface for editing
config.yaml. Auto-detects GPU backend, package manager, and all
repository paths; every value can be overridden.

Usage::

    python3 menuconfig.py
    make menuconfig

Keys:
    ↑ / ↓       Navigate items
    → / Enter   Enter submenu or edit value
    ← / Esc     Return to parent menu
    S           Save config.yaml and exit
    Q           Quit without saving
"""

import curses
import os
import platform
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parent
CONFIG_PATH = REPO_ROOT / "config.yaml"

# ─── Auto-detection ──────────────────────────────────────────────────────────


def _detect_gpu_backend() -> str:
    """Detect best available GPU compute backend.

    macOS: returns ``mps`` when Metal Performance Shaders are available.
    With an eGPU connected, Metal's ``MTLCreateSystemDefaultDevice``
    automatically selects the most capable device (typically the eGPU).
    NVIDIA eGPU on Apple Silicon is experimental; set ``cuda`` manually
    if signed compute drivers are present.

    Linux/Windows: returns ``cuda`` for NVIDIA (or ROCm using the CUDA
    compatibility layer, distinguished by ``torch.version.hip``), else
    ``cpu``.
    """
    if platform.system() == "Darwin":
        try:
            import torch
            if torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            if getattr(torch.version, "hip", None) is not None:
                return "rocm"
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def _detect_package_manager() -> str:
    """Detect the system package manager."""
    if platform.system() == "Darwin":
        return "homebrew"
    for pm in ("apt-get", "dnf", "pacman", "zypper"):
        if subprocess.run(["which", pm], capture_output=True).returncode == 0:
            return "apt" if pm == "apt-get" else pm
    return "unknown"


def _detect_homebrew_prefix() -> str:
    """Detect the Homebrew installation prefix."""
    result = subprocess.run(["brew", "--prefix"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    for candidate in ("/opt/homebrew", "/usr/local"):
        if Path(candidate, "bin", "brew").exists():
            return candidate
    return ""


def _build_defaults() -> dict:
    """Return a config dict populated entirely by auto-detection."""
    root = str(REPO_ROOT)
    scorers = f"{root}/loop/scorers"
    gpu_backend = _detect_gpu_backend()
    pkg_mgr = _detect_package_manager()
    homebrew_prefix = _detect_homebrew_prefix() if pkg_mgr == "homebrew" else ""

    return {
        "paths": {
            "root": root,
            "comfyui": f"{root}/loop/ComfyUI",
            "output": f"{root}/loop/output",
            "scorers": scorers,
        },
        "models": {
            "vlm": {
                "dir": f"{scorers}/models/vlm",
                "mlx_model_name": "Qwen3-VL-8B-Instruct-abliterated-v2",
                "context_length": 4096,
            },
            "artifact_detector": {
                "dir": f"{scorers}/models/artifact-detector",
            },
            "clip": {
                "name": "ViT-L-14",
                "pretrained": "openai",
            },
        },
        "thresholds": {
            "clip": 0.25,
            "artifact": 0.50,
            "score_timeout_secs": 60,
        },
        "broker": {
            "url":             "tcp://localhost:12008",
            "artemis_console": "http://localhost:12009",
            "comfyui_url":     "http://127.0.0.1:12006",
            "comfyui_ws":      "ws://127.0.0.1:12006/ws",
        },
        "database": {
            "postgres_url":      "jdbc:postgresql://localhost:12007/pipeline",
            "postgres_user":     "pipeline",
            "postgres_password": "pipeline",
        },
        "system": {
            "package_manager": pkg_mgr,
            "homebrew_prefix": homebrew_prefix,
        },
        "tactical": {
            "model": {
                "dir":            f"{scorers}/models/tactical",
                "context_length": 8192,
                "temperature":    0.1,
                "max_tokens":     128,
                "server_url":     "http://localhost:12001/v1",
            },
            "decisions": {
                "max_retries":         3,
                "max_inpaints":        2,
                "accept_vlm_mean_min": 7.0,
                "accept_clip_min":     0.30,
            },
        },
        "compute": {
            "comfyui":         {"backend": gpu_backend},
            "clip_scorer":     {"backend": gpu_backend},
            "artifact_scorer": {"backend": gpu_backend},
            "vlm_scorer":      {"backend": gpu_backend},
            "tactical_llm":    {"backend": "auto"},
            "strategic_llm":   {"backend": "auto"},
        },
    }


# ─── Menu schema ─────────────────────────────────────────────────────────────
#
# Each item is a dict with:
#   label      display string
#   key        yaml key within the current section
#   type       "string" | "int" | "float" | "choice"   (leaf nodes)
#   children   list of sub-items                         (section nodes)
#   choices    list of valid strings                     (type=="choice" only)
#   help       one-line description shown in help bar

MENU_SCHEMA = [
    {
        "label": "Paths",
        "key": "paths",
        "help": "Repository and output directory locations",
        "children": [
            {"label": "Repository root",  "key": "root",    "type": "string",
             "help": "Absolute path to the ai-image repository root"},
            {"label": "ComfyUI root",     "key": "comfyui", "type": "string",
             "help": "Path to the ComfyUI installation directory"},
            {"label": "Output directory", "key": "output",  "type": "string",
             "help": "Path where ComfyUI saves generated images"},
            {"label": "Scorers root",     "key": "scorers", "type": "string",
             "help": "Path to loop/scorers (Python ML sidecar sources and models)"},
        ],
    },
    {
        "label": "Models",
        "key": "models",
        "help": "Model file locations and inference parameters",
        "children": [
            {
                "label": "VLM (Qwen3-VL-8B)",
                "key": "vlm",
                "help": "Visual language model for holistic image evaluation",
                "children": [
                    {"label": "Model directory",       "key": "dir",            "type": "string",
                     "help": "Directory containing the VLM model files"},
                    {"label": "MLX model name (macOS)", "key": "mlx_model_name", "type": "string",
                     "help": "Subdirectory name within dir/ for mlx-vlm (macOS only)"},
                    {"label": "Context length",        "key": "context_length", "type": "int",
                     "help": "Maximum context window in tokens (default 4096)"},
                ],
            },
            {
                "label": "Artifact detector",
                "key": "artifact_detector",
                "help": "HuggingFace AI-image-detector classifier",
                "children": [
                    {"label": "Model directory", "key": "dir", "type": "string",
                     "help": "Directory containing the artifact detector model"},
                ],
            },
            {
                "label": "CLIP",
                "key": "clip",
                "help": "OpenAI CLIP model for semantic similarity scoring",
                "children": [
                    {"label": "Model name",  "key": "name",       "type": "string",
                     "help": "open_clip model name (e.g. ViT-L-14)"},
                    {"label": "Pretrained",  "key": "pretrained", "type": "string",
                     "help": "Pretrained weights source (e.g. openai)"},
                ],
            },
        ],
    },
    {
        "label": "Thresholds",
        "key": "thresholds",
        "help": "Scorer acceptance thresholds (see ARCHITECTURE.tex §Threshold Calibration)",
        "children": [
            {"label": "CLIP minimum",     "key": "clip",      "type": "float",
             "help": "Minimum CLIP cosine similarity (0–1). Below this: reject immediately."},
            {"label": "Artifact maximum", "key": "artifact",  "type": "float",
             "help": "Maximum AI-artifact confidence (0–1). Above this: reject after CLIP passes."},
        ],
    },
    {
        "label": "Broker",
        "key": "broker",
        "help": "Artemis and ComfyUI connection parameters",
        "children": [
            {"label": "Artemis URL (OpenWire)", "key": "url",             "type": "string",
             "help": "Artemis OpenWire TCP URL used by the Java pipeline. "
                     "Forwarded by middleware.sh to localhost:12008. "
                     "Remote cluster: set to the broker's external address."},
            {"label": "Artemis console",        "key": "artemis_console", "type": "string",
             "help": "Hawtio management console URL"},
            {"label": "ComfyUI URL",            "key": "comfyui_url",     "type": "string",
             "help": "ComfyUI HTTP API base URL"},
            {"label": "ComfyUI WS",             "key": "comfyui_ws",      "type": "string",
             "help": "ComfyUI WebSocket URL for completion events"},
        ],
    },
    {
        "label": "Tactical LLM",
        "key": "tactical",
        "help": "Per-image decision engine: accept / retry / inpaint",
        "children": [
            {
                "label": "Model",
                "key": "model",
                "help": "Tactical LLM — mlx_lm.server (macOS) or llama_cpp.server (Linux)",
                "children": [
                    {"label": "Model directory",  "key": "dir",            "type": "string",
                     "help": "Directory containing the tactical LLM model files"},
                    {"label": "Context length",   "key": "context_length", "type": "int",
                     "help": "Token context window (default 8192)"},
                    {"label": "Temperature",      "key": "temperature",    "type": "float",
                     "help": "Sampling temperature (0.1 = deterministic)"},
                    {"label": "Max tokens",       "key": "max_tokens",     "type": "int",
                     "help": "Maximum tokens in LLM decision response (default 128)"},
                    {"label": "Server URL",       "key": "server_url",     "type": "string",
                     "help": "OpenAI-compatible API base URL (default http://localhost:12001/v1)"},
                ],
            },
            {
                "label": "Decisions",
                "key": "decisions",
                "help": "Per-session budget and acceptance thresholds",
                "children": [
                    {"label": "Max retries",        "key": "max_retries",        "type": "int",
                     "help": "Maximum retry generations per session"},
                    {"label": "Max inpaints",       "key": "max_inpaints",       "type": "int",
                     "help": "Maximum inpaint passes per session"},
                    {"label": "VLM mean minimum",   "key": "accept_vlm_mean_min","type": "float",
                     "help": "Minimum mean VLM score (0–10) to accept a candidate image"},
                    {"label": "CLIP minimum",       "key": "accept_clip_min",    "type": "float",
                     "help": "Minimum CLIP score (0–1) to accept a candidate image"},
                ],
            },
        ],
    },
    {
        "label": "Database",
        "key": "database",
        "help": "PostgreSQL connection parameters (consumed by Spring Boot application.yml)",
        "children": [
            {"label": "PostgreSQL URL",      "key": "postgres_url",      "type": "string",
             "help": "JDBC URL, e.g. jdbc:postgresql://localhost:12007/pipeline"},
            {"label": "PostgreSQL user",     "key": "postgres_user",     "type": "string",
             "help": "PostgreSQL username"},
            {"label": "PostgreSQL password", "key": "postgres_password", "type": "string",
             "help": "PostgreSQL password"},
        ],
    },
    {
        "label": "System",
        "key": "system",
        "help": "Host system parameters",
        "children": [
            {
                "label": "Package manager",
                "key": "package_manager",
                "type": "choice",
                "choices": ["homebrew", "apt", "dnf", "pacman", "zypper", "unknown"],
                "help": "System package manager",
            },
            {
                "label": "Homebrew prefix",
                "key": "homebrew_prefix",
                "type": "string",
                "help": "/opt/homebrew (Apple Silicon) or /usr/local (Intel Mac). Empty on Linux.",
            },
        ],
    },
    {
        "label": "Compute",
        "key": "compute",
        "help": "Per-component GPU backend and MoE offload settings",
        "children": [
            {
                "label": "Tactical LLM",
                "key": "tactical_llm",
                "help": "Compute backend for tactical LLM (mlx_lm on macOS, llama_cpp on Linux)",
                "children": [
                    {
                        "label": "Backend",
                        "key": "backend",
                        "type": "choice",
                        "choices": ["auto", "mps", "cuda", "rocm", "cpu"],
                        "help": "GPU backend: auto detects at startup",
                    },
                ],
            },
            {
                "label": "VLM scorer",
                "key": "vlm_scorer",
                "help": "Runtime compute options for the VLM scorer",
                "children": [
                    {
                        "label": "Backend",
                        "key": "backend",
                        "type": "choice",
                        "choices": ["auto", "mps", "cuda", "rocm", "cpu"],
                        "help": "GPU backend: auto detects at startup",
                    },
                    {"label": "GPU layers", "key": "n_gpu_layers", "type": "int",
                     "help": "-1 = all layers on GPU; 0 = CPU only"},
                ],
            },
        ],
    },
]


# ─── Config I/O ──────────────────────────────────────────────────────────────


def _load_existing() -> dict | None:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    return None


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge *override* into *base*, recursing into nested dicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _get_nested(cfg: dict, *keys: str) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _set_nested(cfg: dict, value: Any, *keys: str) -> None:
    node = cfg
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def save_config(cfg: dict) -> None:
    """Write config.yaml."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


# ─── Curses UI ───────────────────────────────────────────────────────────────

# Color pair indices
_CP_TITLE    = 1
_CP_SELECTED = 2
_CP_HELP     = 3
_CP_FOOTER   = 4
_CP_AUTO     = 5
_CP_BORDER   = 6
_CP_SECTION  = 7


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_CP_TITLE,    curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(_CP_SELECTED, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(_CP_HELP,     curses.COLOR_CYAN,  -1)
    curses.init_pair(_CP_FOOTER,   curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(_CP_AUTO,     curses.COLOR_YELLOW, -1)
    curses.init_pair(_CP_BORDER,   curses.COLOR_CYAN,  -1)
    curses.init_pair(_CP_SECTION,  curses.COLOR_WHITE, -1)


def _safe_addstr(win, y: int, x: int, s: str, attr: int = 0) -> None:
    """addstr wrapper that swallows the out-of-bounds exception."""
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


class MenuUI:
    """Kernel-menuconfig-style curses interface.

    State is a navigation stack of ``(schema_items, selected_index,
    key_path)`` tuples. Entering a submenu pushes; Esc/Left pops.
    """

    def __init__(self, stdscr, cfg: dict, auto_defaults: dict) -> None:
        self.stdscr = stdscr
        self.cfg = cfg
        self.auto_defaults = auto_defaults
        # Stack: list of (items: list, idx: int, key_path: list[str])
        self.stack: list[tuple[list, int, list]] = [(MENU_SCHEMA, 0, [])]
        self.status: str = ""

    # ── Stack helpers ─────────────────────────────────────────────────

    def _push(self, items: list, key_path: list) -> None:
        self.stack.append((items, 0, key_path))

    def _pop(self) -> bool:
        if len(self.stack) > 1:
            self.stack.pop()
            return True
        return False

    @property
    def _items(self) -> list:
        return self.stack[-1][0]

    @property
    def _idx(self) -> int:
        return self.stack[-1][1]

    @_idx.setter
    def _idx(self, v: int) -> None:
        items, _, path = self.stack[-1]
        self.stack[-1] = (items, v, path)

    @property
    def _key_path(self) -> list:
        return self.stack[-1][2]

    def _full_path(self, item: dict) -> list:
        return self._key_path + [item["key"]]

    def _get_val(self, item: dict) -> Any:
        return _get_nested(self.cfg, *self._full_path(item))

    def _set_val(self, item: dict, value: Any) -> None:
        _set_nested(self.cfg, value, *self._full_path(item))

    def _is_auto(self, item: dict) -> bool:
        path = self._full_path(item)
        cur = _get_nested(self.cfg, *path)
        det = _get_nested(self.auto_defaults, *path)
        return det is not None and cur == det

    def _breadcrumb(self) -> str:
        parts = [p[-1] for _, _, p in self.stack[1:] if p]
        return " › ".join(parts)

    # ── Drawing ───────────────────────────────────────────────────────

    def _draw(self) -> None:
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()

        # Title bar
        title = "  AI Image Pipeline ─── Configuration  "
        _safe_addstr(s, 0, 0, title.center(w - 1),
                     curses.color_pair(_CP_TITLE) | curses.A_BOLD)

        # Breadcrumb
        bc = self._breadcrumb()
        if bc:
            _safe_addstr(s, 1, 2, f"── {bc}", curses.color_pair(_CP_BORDER))
        _safe_addstr(s, 2, 0, "─" * (w - 1), curses.color_pair(_CP_BORDER))

        # Content area
        content_top = 3
        content_bot = h - 6
        visible = max(1, content_bot - content_top)
        items = self._items
        sel = self._idx
        scroll = max(0, sel - visible // 2)

        for i, item in enumerate(items[scroll: scroll + visible]):
            actual = i + scroll
            y = content_top + i
            is_sel = (actual == sel)
            is_section = "children" in item

            val = self._get_val(item)
            is_auto = self._is_auto(item) if not is_section else False

            if is_section:
                indent = "  "
                label_str = item["label"]
                val_str = "───>"
                base_attr = curses.color_pair(_CP_SECTION) | curses.A_BOLD
            else:
                indent = "    "
                label_str = item["label"]
                if val is None:
                    val_str = "(unset)"
                elif is_auto:
                    val_str = f"{val}  [auto]"
                else:
                    val_str = str(val)
                base_attr = curses.A_NORMAL

            label_col = w - 36
            marker = ">" if is_sel else " "
            line = f"{marker}{indent}{label_str}"
            val_field = val_str[:34]

            if is_sel:
                attr = curses.color_pair(_CP_SELECTED) | curses.A_BOLD
                _safe_addstr(s, y, 0, f" {line:<{label_col - 1}} {val_field:<34} ", attr)
            else:
                _safe_addstr(s, y, 0, f" {line:<{label_col - 1}} ", base_attr)
                val_attr = curses.color_pair(_CP_AUTO) if is_auto else curses.A_DIM
                _safe_addstr(s, y, label_col, f"{val_field:<34}", val_attr)

        # Help area
        _safe_addstr(s, h - 5, 0, "─" * (w - 1), curses.color_pair(_CP_BORDER))
        if items and sel < len(items):
            help_text = items[sel].get("help", "")
            wrapped = textwrap.wrap(help_text, w - 4)
            for li, line in enumerate(wrapped[:2]):
                _safe_addstr(s, h - 4 + li, 2, line, curses.color_pair(_CP_HELP))

        # Status / footer
        _safe_addstr(s, h - 2, 0, "─" * (w - 1), curses.color_pair(_CP_BORDER))
        if self.status:
            _safe_addstr(s, h - 2, 2, self.status[:w - 4],
                         curses.color_pair(_CP_AUTO) | curses.A_BOLD)
        footer = ("  ↑↓ Navigate   → / ↵ Enter/Edit   ← / Esc Back"
                  "   S Save & Exit   Q Quit  ")
        _safe_addstr(s, h - 1, 0, footer.ljust(w - 1)[:w - 1],
                     curses.color_pair(_CP_FOOTER))
        s.refresh()

    # ── Editing ───────────────────────────────────────────────────────

    def _edit_string(self, item: dict) -> None:
        """Inline bottom-bar editor for string / int / float fields."""
        h, w = self.stdscr.getmaxyx()
        current = self._get_val(item)
        current_str = "" if current is None else str(current)
        prompt = f" {item['label']}: "

        curses.echo()
        curses.curs_set(1)
        edit_y = h - 2

        _safe_addstr(self.stdscr, edit_y, 0, " " * (w - 1),
                     curses.color_pair(_CP_AUTO) | curses.A_BOLD)
        _safe_addstr(self.stdscr, edit_y, 0, prompt,
                     curses.color_pair(_CP_AUTO) | curses.A_BOLD)
        _safe_addstr(self.stdscr, edit_y, len(prompt), current_str)
        self.stdscr.refresh()

        max_len = max(1, w - len(prompt) - 2)
        try:
            raw = self.stdscr.getstr(edit_y, len(prompt), max_len).decode("utf-8").strip()
        except Exception:
            raw = current_str

        curses.noecho()
        curses.curs_set(0)

        if not raw:
            return  # keep old value on empty input

        itype = item.get("type", "string")
        try:
            if itype == "int":
                self._set_val(item, int(raw))
            elif itype == "float":
                self._set_val(item, float(raw))
            else:
                self._set_val(item, raw)
            self.status = f"Set {item['label']} = {raw}"
        except ValueError:
            self.status = f"Invalid {itype}: {raw!r} — value unchanged"

    def _edit_choice(self, item: dict) -> None:
        """Popup selector for choice fields."""
        choices = item.get("choices", [])
        current = self._get_val(item)
        sel = choices.index(current) if current in choices else 0
        h, w = self.stdscr.getmaxyx()

        popup_w = max(len(item["label"]) + 6,
                      max(len(c) for c in choices) + 12,
                      32)
        popup_h = len(choices) + 4
        py = max(3, (h - popup_h) // 2)
        px = max(0, (w - popup_w) // 2)
        popup = curses.newwin(popup_h, popup_w, py, px)

        auto_val = _get_nested(self.auto_defaults, *self._full_path(item))

        while True:
            popup.erase()
            popup.box()
            label = f" {item['label']} "
            _safe_addstr(popup, 0, max(2, (popup_w - len(label)) // 2),
                         label, curses.color_pair(_CP_TITLE) | curses.A_BOLD)

            for i, choice in enumerate(choices):
                is_sel = (i == sel)
                suffix = "  [auto]" if choice == auto_val else ""
                marker = "●" if is_sel else " "
                line = f" {marker} {choice}{suffix}"
                attr = (curses.color_pair(_CP_SELECTED) | curses.A_BOLD
                        if is_sel else curses.A_NORMAL)
                _safe_addstr(popup, i + 2, 1, f"{line:<{popup_w - 2}}", attr)

            popup.refresh()
            key = popup.getch()

            if key == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif key == curses.KEY_DOWN:
                sel = min(len(choices) - 1, sel + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                self._set_val(item, choices[sel])
                self.status = f"Set {item['label']} = {choices[sel]}"
                break
            elif key in (27, curses.KEY_LEFT):
                break

        del popup
        self.stdscr.touchwin()

    def _confirm_save(self) -> bool:
        """Modal yes/no save confirmation dialog."""
        h, w = self.stdscr.getmaxyx()
        msg = "  Save config.yaml?  [Y / n]  "
        dw = len(msg) + 4
        dh = 3
        dy = (h - dh) // 2
        dx = max(0, (w - dw) // 2)
        dlg = curses.newwin(dh, dw, dy, dx)
        dlg.box()
        _safe_addstr(dlg, 1, 2, msg, curses.color_pair(_CP_AUTO) | curses.A_BOLD)
        dlg.refresh()
        key = dlg.getch()
        del dlg
        self.stdscr.touchwin()
        return key in (ord("y"), ord("Y"), 10, 13)

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> dict | None:
        """Run the TUI event loop. Returns saved config dict or None."""
        curses.curs_set(0)
        _init_colors()

        while True:
            self._draw()
            key = self.stdscr.getch()
            self.status = ""
            items = self._items
            idx = self._idx

            if key == curses.KEY_UP:
                self._idx = max(0, idx - 1)

            elif key == curses.KEY_DOWN:
                self._idx = min(len(items) - 1, idx + 1)

            elif key in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13):
                if not items:
                    continue
                item = items[idx]
                if "children" in item:
                    self._push(item["children"],
                               self._key_path + [item["key"]])
                elif item.get("type") == "choice":
                    self._edit_choice(item)
                else:
                    self._edit_string(item)

            elif key in (curses.KEY_LEFT, 27):  # Left arrow or Esc
                self._pop()

            elif key in (ord("s"), ord("S")):
                if self._confirm_save():
                    return self.cfg

            elif key in (ord("q"), ord("Q")):
                return None

        return None  # unreachable


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    auto_defaults = _build_defaults()
    existing = _load_existing()
    cfg = _deep_merge(auto_defaults, existing) if existing else auto_defaults

    result = curses.wrapper(lambda s: MenuUI(s, cfg, auto_defaults).run())

    if result is not None:
        save_config(result)
        print(f"Saved: {CONFIG_PATH}")
        print()
        print(f"GPU backend:     {result['gpu']['backend']}")
        print(f"Package manager: {result['system']['package_manager']}")
        print(f"Repo root:       {result['paths']['root']}")
        print()
        print("Run  python check_env.py  to validate the environment.")
    else:
        print("Aborted — no changes saved.")


if __name__ == "__main__":
    main()
