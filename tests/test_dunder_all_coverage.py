"""Lift N defensive pattern #1 — ``__all__`` coverage invariant.

Every ``__init__.py`` under ``backend/`` / ``bsvibe_sdk/`` / ``plugin/``
must declare an explicit ``__all__`` (even if it's the empty list).
This makes the public surface intentional rather than implicit, and
gives future Lift N-Coverage tooling a deterministic ground truth.

The check is structural — ``re``-based, not an ``import`` — so a
syntactically broken file flags here without crashing the test session.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("backend", "bsvibe_sdk", "plugin")
ALL_PATTERN = re.compile(r"^__all__\b", re.MULTILINE)


def test_every_init_py_declares_dunder_all() -> None:
    missing: list[str] = []
    for root in SCAN_ROOTS:
        for init in (REPO_ROOT / root).rglob("__init__.py"):
            text = init.read_text(encoding="utf-8")
            if not ALL_PATTERN.search(text):
                missing.append(str(init.relative_to(REPO_ROOT)))
    assert not missing, (
        "Lift N defensive pattern #1 (v8 §22): every __init__.py must "
        "declare __all__. Missing in:\n  - " + "\n  - ".join(missing)
    )
