#!/usr/bin/env python3
"""HuggingFace model picker for the AI image pipeline.

Without HF_TOKEN: prints browse URLs and file drop locations.
With HF_TOKEN:    launches a curses TUI for searching, browsing, and
                  selecting models; writes the chosen filename to config.yaml.

Usage::

    python3 model_picker.py
    HF_TOKEN=hf_... python3 model_picker.py

Keys (interactive mode):
    ↑ / ↓       Navigate list
    Enter       Confirm / drill into repo
    Backspace   Delete search character
    Esc         Back to previous screen
    q           Quit
"""

import curses
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent

# ── Model categories ──────────────────────────────────────────────────────────

CATEGORIES: list[dict[str, Any]] = [
    {
        "name":           "SDXL Checkpoint",
        "description":    "Image generation model for ComfyUI",
        "default_search": "sdxl",
        "search_kwargs":  {"pipeline_tag": "text-to-image"},
        "extensions":     [".safetensors", ".ckpt"],
        "drop_path":      "loop/ComfyUI/models/checkpoints/",
        "config_key":     None,  # filename lives in workflow JSON, not config.yaml
        "browse_url":     "https://huggingface.co/models?pipeline_tag=text-to-image&sort=downloads",
    },
    {
        "name":           "VLM Scorer",
        "description":    "Vision-language model for holistic image evaluation",
        "default_search": "VL instruct",
        "search_kwargs":  {"library": "gguf"},
        "extensions":     [".gguf"],
        "drop_path":      "loop/scorers/models/vlm/",
        "config_key":     "models.vlm.filename",
        "browse_url":     "https://huggingface.co/models?library=gguf&sort=downloads&search=VL+instruct",
    },
    {
        "name":           "Tactical LLM",
        "description":    "Language model for prompt revision and routing decisions",
        "default_search": "instruct",
        "search_kwargs":  {"library": "gguf"},
        "extensions":     [".gguf"],
        "drop_path":      "loop/scorers/models/tactical/",
        "config_key":     "tactical.model.filename",
        "browse_url":     "https://huggingface.co/models?library=gguf&sort=downloads&search=instruct",
    },
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024  # type: ignore[assignment]
    return f"{n_bytes:.1f} PB"


def _quant_label(filename: str) -> str:
    """Return a human-readable label for common GGUF quantisation suffixes."""
    upper = filename.upper()
    for suffix, label in [
        ("Q8_0",   "8-bit"),
        ("Q6_K",   "6-bit"),
        ("Q5_K_M", "5-bit medium"),
        ("Q5_K_S", "5-bit small"),
        ("Q5_0",   "5-bit"),
        ("Q4_K_M", "4-bit medium"),
        ("Q4_K_S", "4-bit small"),
        ("Q4_0",   "4-bit"),
        ("IQ4_XS", "4-bit xs"),
        ("IQ3_M",  "3-bit medium"),
        ("BF16",   "16-bit bfloat"),
        ("F16",    "16-bit float"),
    ]:
        if suffix in upper:
            return label
    return ""


def _write_config(config_key: str, value: str) -> bool:
    """Update a dotted key in config.yaml (or config.yaml.default) using yq."""
    path = REPO_ROOT / "config.yaml"
    if not path.exists():
        path = REPO_ROOT / "config.yaml.default"
    try:
        subprocess.run(
            ["yq", "-i", f'.{config_key} = "{value}"', str(path)],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

# ── No-token path ─────────────────────────────────────────────────────────────

def show_no_token() -> None:
    """Print browse URLs and file drop locations when HF_TOKEN is not set."""
    w = 72
    print()
    print("  HF_TOKEN is not set — interactive model search unavailable.")
    print()
    print("  Browse and download models manually, then drop them in the")
    print("  locations below and update config.yaml as noted.")
    print()
    print("  " + "─" * w)
    for cat in CATEGORIES:
        print()
        print(f"  {cat['name']}  —  {cat['description']}")
        print(f"    Browse:    {cat['browse_url']}")
        print(f"    Drop into: {REPO_ROOT / cat['drop_path']}")
        if cat["config_key"]:
            print(f"    config.yaml key: {cat['config_key']}")
        else:
            print( "    (no config.yaml key — filename is set in the workflow JSON)")
    print()
    print("  " + "─" * w)
    print()
    print("  Set HF_TOKEN in your environment to enable interactive search:")
    print("    export HF_TOKEN=hf_...")
    print()

# ── Curses UI ─────────────────────────────────────────────────────────────────

class ModelPicker:
    """Curses-based HuggingFace model browser."""

    _STATE_CATEGORY = "category"
    _STATE_SEARCH   = "search"
    _STATE_FILES    = "files"
    _STATE_CONFIRM  = "confirm"

    # Colour pair indices
    _C_HEADER  = 1
    _C_SEL     = 2
    _C_LABEL   = 3
    _C_DIM     = 4
    _C_SUCCESS = 5
    _C_ERROR   = 6

    def __init__(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        self._init_colors()
        curses.curs_set(0)
        stdscr.timeout(100)  # ms; lets background threads update the display

        from huggingface_hub import HfApi
        self._api = HfApi()

        self._state       = self._STATE_CATEGORY
        self._cat_idx     = 0
        self._category: dict[str, Any] | None = None

        self._query       = ""
        self._results: list[Any] = []
        self._res_idx     = 0

        self._repo_id     = ""
        self._files: list[dict[str, Any]] = []
        self._file_idx    = 0

        self._loading     = False
        self._status      = ""
        self._error       = ""

    def _init_colors(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(self._C_HEADER,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
        curses.init_pair(self._C_SEL,     curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(self._C_LABEL,   curses.COLOR_CYAN,   -1)
        curses.init_pair(self._C_DIM,     curses.COLOR_WHITE,  -1)
        curses.init_pair(self._C_SUCCESS, curses.COLOR_GREEN,  -1)
        curses.init_pair(self._C_ERROR,   curses.COLOR_RED,    -1)

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        while True:
            self._draw()
            key = self.stdscr.getch()
            if key == -1:
                continue
            if self._dispatch_key(key) == "quit":
                break

    # ── Drawing ────────────────────────────────────────────────────────────────

    def _draw(self) -> None:
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()
        self._draw_header(w)
        if self._state == self._STATE_CATEGORY:
            self._draw_categories(h, w)
        elif self._state == self._STATE_SEARCH:
            self._draw_search(h, w)
        elif self._state == self._STATE_FILES:
            self._draw_files(h, w)
        elif self._state == self._STATE_CONFIRM:
            self._draw_confirm(h, w)
        self._draw_statusbar(h, w)
        self.stdscr.refresh()

    def _draw_header(self, w: int) -> None:
        title = " AI Image Pipeline — Model Picker "
        self._puts(0, 0, title.center(w), self._C_HEADER)

    def _draw_categories(self, h: int, w: int) -> None:
        self._puts(2, 2, "Select model to configure:", self._C_LABEL)
        for i, cat in enumerate(CATEGORIES):
            y = 4 + i * 2
            attr = curses.color_pair(self._C_SEL) if i == self._cat_idx else curses.A_NORMAL
            self._puts(y, 4, f"  {cat['name']:<28}  {cat['description']}", pair=0, extra=attr)

    def _draw_search(self, h: int, w: int) -> None:
        assert self._category is not None
        self._puts(2, 2, f"Model: {self._category['name']}", self._C_LABEL)
        self._puts(4, 2, f"Search: {self._query}_", pair=0, extra=curses.A_BOLD)

        if self._loading:
            self._puts(6, 4, "Searching HuggingFace…", self._C_LABEL)
            return

        if self._error:
            self._puts(6, 4, self._error, self._C_ERROR)
            return

        if not self._results:
            self._puts(6, 4, "Type a search term and press Enter.", self._C_DIM)
            return

        visible = h - 9
        start   = max(0, self._res_idx - visible // 2)
        for i, model in enumerate(self._results[start:start + visible]):
            idx  = start + i
            attr = curses.color_pair(self._C_SEL) if idx == self._res_idx else curses.A_NORMAL
            dl   = f"{model.downloads:,}" if getattr(model, "downloads", None) else "?"
            line = f"  {model.modelId:<52} ↓{dl:>10}"
            self._puts(6 + i, 2, line[:w - 3], pair=0, extra=attr)

    def _draw_files(self, h: int, w: int) -> None:
        self._puts(2, 2, f"Repo: {self._repo_id}", self._C_LABEL)

        if self._loading:
            self._puts(4, 4, "Fetching file list…", self._C_LABEL)
            return

        if self._error:
            self._puts(4, 4, self._error, self._C_ERROR)
            return

        if not self._files:
            self._puts(4, 4, "No matching files found in this repository.", self._C_DIM)
            return

        visible = h - 7
        start   = max(0, self._file_idx - visible // 2)
        for i, f in enumerate(self._files[start:start + visible]):
            idx   = start + i
            attr  = curses.color_pair(self._C_SEL) if idx == self._file_idx else curses.A_NORMAL
            sz    = _human_size(f["size"]) if f["size"] else "?"
            quant = _quant_label(f["name"])
            qlbl  = f"  [{quant}]" if quant else ""
            line  = f"  {f['name']:<58} {sz:>9}{qlbl}"
            self._puts(4 + i, 2, line[:w - 3], pair=0, extra=attr)

    def _draw_confirm(self, h: int, w: int) -> None:
        pair = self._C_SUCCESS if not self._error else self._C_ERROR
        msg  = self._status or self._error
        self._puts(3, 4, msg, pair)
        self._puts(5, 4, "Press any key to continue.", self._C_DIM)

    def _draw_statusbar(self, h: int, w: int) -> None:
        hints = {
            self._STATE_CATEGORY: "↑↓ navigate   Enter select   q quit",
            self._STATE_SEARCH:   "↑↓ navigate results   Enter fetch/select   Esc back   q quit",
            self._STATE_FILES:    "↑↓ navigate   Enter select   Esc back   q quit",
            self._STATE_CONFIRM:  "Any key to continue",
        }
        bar = hints.get(self._state, "")
        self._puts(h - 1, 0, bar.ljust(w - 1), self._C_LABEL)

    def _puts(self, y: int, x: int, text: str, pair: int = 0,
              extra: int = curses.A_NORMAL) -> None:
        try:
            h, w = self.stdscr.getmaxyx()
            if y < 0 or y >= h:
                return
            self.stdscr.addstr(y, x, text[:w - x - 1],
                               curses.color_pair(pair) | extra)
        except curses.error:
            pass

    # ── Key dispatch ───────────────────────────────────────────────────────────

    def _dispatch_key(self, key: int) -> str | None:
        if self._state == self._STATE_CATEGORY:
            return self._key_category(key)
        if self._state == self._STATE_SEARCH:
            return self._key_search(key)
        if self._state == self._STATE_FILES:
            return self._key_files(key)
        if self._state == self._STATE_CONFIRM:
            self._state = self._STATE_CATEGORY
            self._status = self._error = ""
        return None

    def _key_category(self, key: int) -> str | None:
        if key in (ord("q"), ord("Q"), 27):
            return "quit"
        if key == curses.KEY_UP:
            self._cat_idx = max(0, self._cat_idx - 1)
        elif key == curses.KEY_DOWN:
            self._cat_idx = min(len(CATEGORIES) - 1, self._cat_idx + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            self._category   = CATEGORIES[self._cat_idx]
            self._query      = self._category.get("default_search", "")
            self._results    = []
            self._res_idx    = 0
            self._error      = ""
            self._state      = self._STATE_SEARCH
            if self._query:
                self._start_search()
        return None

    def _key_search(self, key: int) -> str | None:
        if key == 27:  # Esc
            self._state = self._STATE_CATEGORY
        elif key in (ord("q"), ord("Q")) and not self._query:
            return "quit"
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self._query   = self._query[:-1]
            self._results = []
            self._res_idx = 0
            self._error   = ""
        elif key in (curses.KEY_ENTER, 10, 13):
            if self._results and not self._loading:
                self._open_repo(self._results[self._res_idx].modelId)
            elif not self._loading:
                self._start_search()
        elif key == curses.KEY_UP:
            self._res_idx = max(0, self._res_idx - 1)
        elif key == curses.KEY_DOWN:
            self._res_idx = min(max(0, len(self._results) - 1), self._res_idx + 1)
        elif 32 <= key <= 126:
            self._query  += chr(key)
            self._results = []
            self._res_idx = 0
            self._error   = ""
        return None

    def _key_files(self, key: int) -> str | None:
        if key == 27:
            self._state = self._STATE_SEARCH
        elif key in (ord("q"), ord("Q")):
            return "quit"
        elif key == curses.KEY_UP:
            self._file_idx = max(0, self._file_idx - 1)
        elif key == curses.KEY_DOWN:
            self._file_idx = min(max(0, len(self._files) - 1), self._file_idx + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if self._files and not self._loading:
                self._apply(self._files[self._file_idx])
        return None

    # ── Background API calls ───────────────────────────────────────────────────

    def _start_search(self) -> None:
        if self._loading or not self._query:
            return
        self._loading = True
        query  = self._query
        kwargs = (self._category or {}).get("search_kwargs", {})

        def _work() -> None:
            try:
                self._results = list(self._api.list_models(
                    search=query,
                    sort="downloads",
                    direction=-1,
                    limit=50,
                    **kwargs,
                ))
                self._res_idx = 0
                self._error   = ""
            except Exception as exc:
                self._error   = f"Search failed: {exc}"
                self._results = []
            finally:
                self._loading = False

        threading.Thread(target=_work, daemon=True).start()

    def _open_repo(self, repo_id: str) -> None:
        self._repo_id  = repo_id
        self._files    = []
        self._file_idx = 0
        self._error    = ""
        self._loading  = True
        self._state    = self._STATE_FILES
        exts = (self._category or {}).get("extensions", [])

        def _work() -> None:
            try:
                items = self._api.list_repo_tree(repo_id, expand=True)
                files = []
                for item in items:
                    path = getattr(item, "path", "")
                    size = getattr(item, "size", 0) or 0
                    if any(path.lower().endswith(e) for e in exts):
                        files.append({"name": Path(path).name, "path": path, "size": size})
                self._files    = sorted(files, key=lambda f: f["name"])
                self._file_idx = 0
                self._error    = ""
            except Exception as exc:
                self._error = f"Could not list files: {exc}"
                self._files = []
            finally:
                self._loading = False

        threading.Thread(target=_work, daemon=True).start()

    # ── Config update ──────────────────────────────────────────────────────────

    def _apply(self, f: dict[str, Any]) -> None:
        assert self._category is not None
        config_key = self._category["config_key"]
        drop_path  = REPO_ROOT / self._category["drop_path"]

        if config_key:
            ok = _write_config(config_key, f["name"])
            if ok:
                self._status = (
                    f"Saved: {config_key} = {f['name']}\n"
                    f"Drop the file into: {drop_path}"
                )
                self._error = ""
            else:
                self._error  = "Failed to write config.yaml — is yq installed?"
                self._status = ""
        else:
            self._status = (
                f"Drop {f['name']} into:\n  {drop_path}\n"
                f"Then update the workflow JSON to reference it."
            )
            self._error = ""

        self._state = self._STATE_CONFIRM


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("HF_TOKEN", "").strip()

    if not token:
        show_no_token()
        return

    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("huggingface_hub is not installed.")
        print("Activate the root venv first:  source venv/bin/activate")
        return

    # Set the token so huggingface_hub picks it up automatically
    os.environ["HF_TOKEN"] = token

    def _run(stdscr: curses.window) -> None:
        picker = ModelPicker(stdscr)
        picker.run()

    curses.wrapper(_run)


if __name__ == "__main__":
    main()
