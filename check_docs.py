"""Documentation consistency checker.

Verifies that key factual claims in MESSAGES.md and ARCHITECTURE.tex match
what the source code actually does.  Run with ``make check-docs`` or directly:

    python check_docs.py

Exits 0 if all checks pass, 1 if any fail.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# ── Helpers ───────────────────────────────────────────────────────────────────

_failures: list[str] = []
_passes:   list[str] = []


def ok(label: str) -> None:
    _passes.append(label)


def fail(label: str, detail: str = "") -> None:
    msg = label + (f": {detail}" if detail else "")
    _failures.append(msg)


def read(rel: str) -> str:
    return (ROOT / rel).read_text()


def grep(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text))


# ── Port numbers ──────────────────────────────────────────────────────────────

def check_ports() -> None:
    messages = read("MESSAGES.md")
    arch     = read("ARCHITECTURE.tex")

    port_map = {
        "pipeline (12000)":          (r"localhost:12000|port 12000",  [messages, arch]),
        "tactical LLM (12001)":      (r"localhost:12001|port 12001", [arch]),
        "clip scorer (12002)":       (r"localhost:12002",   [messages, arch]),
        "artifact scorer (12003)":   (r"localhost:12003",   [messages, arch]),
        "vlm scorer (12004)":        (r"localhost:12004",   [messages, arch]),
        "strategic LLM (12005)":     (r"localhost:12005",   [arch]),
        "ComfyUI (12006)":           (r"12006",              [arch]),
        "Artemis console (12009)":   (r"localhost:12009",   [messages, arch]),
    }

    for label, (pattern, docs) in port_map.items():
        for doc in docs:
            if not grep(doc, pattern):
                fail(f"port {label}", f"pattern '{pattern}' not found")
                return
    ok("port numbers")


# ── Queue names ───────────────────────────────────────────────────────────────

QUEUES_FROM_CODE = [
    "loop.generate",
    "loop.retry",
    "loop.inpaint",
    "loop.generated",
    "loop.verdicts",
    "pipeline.dead",
]

def check_queues() -> None:
    messages = read("MESSAGES.md")
    arch     = read("ARCHITECTURE.tex")

    for q in QUEUES_FROM_CODE:
        for name, doc in [("MESSAGES.md", messages), ("ARCHITECTURE.tex", arch)]:
            if q not in doc:
                fail(f"queue '{q}' missing from {name}")
    ok("queue names")


# ── JMS listeners (from Java source) match docs ───────────────────────────────

def check_listeners() -> None:
    workers_dir = ROOT / "pipeline/src/main/java/org/soxhlet/pipeline/worker"
    java_src = "\n".join(p.read_text() for p in workers_dir.glob("*.java"))

    listeners = re.findall(r'@JmsListener\(destination\s*=\s*"([^"]+)"', java_src)
    publishers = re.findall(r'convertAndSend\("([^"]+)"', java_src)

    messages = read("MESSAGES.md")

    for q in set(listeners):
        if q not in messages:
            fail(f"JMS listener queue '{q}' not documented in MESSAGES.md")

    for q in set(publishers):
        if q not in messages:
            fail(f"JMS publisher queue '{q}' not documented in MESSAGES.md")

    ok("JMS listener/publisher queues documented")


# ── verdict column (not status) used for rejected images ─────────────────────

def check_verdict_column() -> None:
    for fname in ("MESSAGES.md", "ARCHITECTURE.tex"):
        text = read(fname)
        if "status = 'rejected'" in text or "status='rejected'" in text:
            fail(f"{fname}: uses 'status = rejected' — column is 'verdict'")
        else:
            ok(f"{fname}: correct 'verdict' column")


# ── loop.inpaint publisher is NOT TacticalLlmCaller ──────────────────────────

def check_inpaint_publisher() -> None:
    messages = read("MESSAGES.md")
    arch     = read("ARCHITECTURE.tex")

    # MESSAGES.md should say "none" or "not yet implemented" as publisher
    inpaint_section = re.search(
        r"### `loop\.inpaint`.*?(?=###|\Z)", messages, re.DOTALL
    )
    if inpaint_section:
        block = inpaint_section.group(0)
        if "TacticalLlmCaller" in block and "none" not in block.lower():
            fail("MESSAGES.md loop.inpaint: publisher incorrectly listed as TacticalLlmCaller")
        else:
            ok("MESSAGES.md loop.inpaint publisher")
    else:
        fail("MESSAGES.md loop.inpaint: section not found")

    # ARCHITECTURE.tex table row for loop.inpaint should not say TacticalLlmCaller
    row = re.search(r"loop\.inpaint.*?\\\\", arch)
    if row and "TacticalLlmCaller" in row.group(0):
        fail("ARCHITECTURE.tex loop.inpaint table row: publisher incorrectly listed as TacticalLlmCaller")
    else:
        ok("ARCHITECTURE.tex loop.inpaint publisher")


# ── WebSocket event fields ────────────────────────────────────────────────────

def check_ws_events() -> None:
    """image_ready must have conversation_id and sequence_number (not image_path/session_uuid)."""
    messages = read("MESSAGES.md")

    ready_section = re.search(
        r"\*\*`image_ready`\*\*.*?(?=\*\*`decision`\*\*|\Z)", messages, re.DOTALL
    )
    if not ready_section:
        fail("MESSAGES.md: image_ready event section not found")
        return

    block = ready_section.group(0)
    for required in ("conversation_id", "sequence_number"):
        if required not in block:
            fail(f"MESSAGES.md image_ready event: missing '{required}'")
    for wrong in ("image_path", "session_uuid"):
        if wrong in block:
            fail(f"MESSAGES.md image_ready event: should not contain '{wrong}'")

    ok("MESSAGES.md image_ready event fields")

    decision_section = re.search(
        r"\*\*`decision`\*\*.*", messages, re.DOTALL
    )
    if not decision_section:
        fail("MESSAGES.md: decision event section not found")
        return

    dblock = decision_section.group(0)
    for required in ("conversation_id", "reasoning", "confidence"):
        if required not in dblock:
            fail(f"MESSAGES.md decision event: missing '{required}'")

    # The decision event should NOT be a nested object with a "decision" sub-object
    if '"decision": {' in dblock or '"decision":\n    {' in dblock:
        fail("MESSAGES.md decision event: appears to be the old nested-object format")
    else:
        ok("MESSAGES.md decision event fields")


# ── workflow_params schema uses class_type keys ───────────────────────────────

def check_workflow_params() -> None:
    messages = read("MESSAGES.md")

    params_section = re.search(
        r'"workflow_params".*?(?=\n  "[a-z]|\n})', messages, re.DOTALL
    )
    if not params_section:
        fail("MESSAGES.md: workflow_params block not found")
        return

    block = params_section.group(0)
    # Should reference class_type-style keys like KSampler, EmptyLatentImage
    if "KSampler" not in block and "EmptyLatentImage" not in block:
        fail("MESSAGES.md workflow_params: should use class_type keys (KSampler, EmptyLatentImage)")
    # Should NOT have old flat top-level keys (checkpoint/sampler as direct children)
    for bad in ('"checkpoint":', '"sampler":'):
        if bad in block:
            fail(f"MESSAGES.md workflow_params: contains stale flat key {bad}")
    ok("MESSAGES.md workflow_params schema")


# ── prompts.py: no reference to non-existent tactical_llm.py ─────────────────

def check_prompts_docstring() -> None:
    path = ROOT / "loop/tactical-llm/prompts.py"
    if not path.exists():
        ok("prompts.py (not present)")
        return
    text = path.read_text()
    if "tactical_llm.py" in text:
        fail("prompts.py: references tactical_llm.py which does not exist")
    else:
        ok("prompts.py: no stale tactical_llm.py reference")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    check_ports()
    check_queues()
    check_listeners()
    check_verdict_column()
    check_inpaint_publisher()
    check_ws_events()
    check_workflow_params()
    check_prompts_docstring()

    total = len(_passes) + len(_failures)
    print(f"\ncheck_docs: {len(_passes)}/{total} checks passed")

    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print(f"  FAIL  {f}")
        return 1

    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
