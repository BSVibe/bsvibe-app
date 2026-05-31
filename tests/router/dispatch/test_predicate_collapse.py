"""Lift D — assert the executor predicate scatter is collapsed.

Before Lift D the literal ``provider == "executor"`` (and its inverse) appeared
in scattered call sites — every one re-deciding what an executor account is.
Lift D moves the invariant check to
:func:`backend.router.dispatch.strategies.is_executor_account`; the only
remaining literal-column comparisons live where the column is filtered as
data (SQL filters in the accounts repository + the worker-health probe) and
use the :data:`EXECUTOR_PROVIDER` constant — NOT a hard-coded string.

This test pins the collapse by greping the production source. If a future
change re-introduces a scattered literal predicate the test will fail,
forcing the author to either route through ``is_executor_account`` or to
update this test with a written-down justification.
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo root — this test file lives at tests/router/dispatch/test_*.py so the
# repo root is the test file's 4th parent (file → dispatch → router → tests
# → repo root).
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"

# Files allowed to hold a ``provider == "executor"`` literal — the strategy
# module that DEFINES the predicate. Any other occurrence is a scatter
# regression.
_ALLOWED = {
    BACKEND / "router" / "dispatch" / "strategies" / "__init__.py",
    BACKEND / "router" / "dispatch" / "strategies" / "cli_wrapper.py",
}

# Regex catches the invariant-style predicate (==, !=) with the literal string.
_PREDICATE = re.compile(r'provider\s*[!=]=\s*"executor"')


def _iter_py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_scattered_executor_predicate_in_backend() -> None:
    """Every former scatter site routes through is_executor_account /
    EXECUTOR_PROVIDER now. Only the strategy module DEFINES the constant + the
    predicate (its docstrings mention ``provider == "executor"`` as context)."""
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_py_files(BACKEND):
        if path in _ALLOWED:
            continue
        # Migrations + the dispatch shim are allowed: migrations bake in
        # historical values; the shim re-exports.
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PREDICATE.search(line):
                offenders.append((path.relative_to(REPO_ROOT), lineno, line.strip()))
    assert offenders == [], (
        'scattered ``provider == "executor"`` predicate(s) found — route through '
        "backend.router.dispatch.strategies.is_executor_account / EXECUTOR_PROVIDER:\n"
        + "\n".join(f"  {p}:{ln}  {body}" for p, ln, body in offenders)
    )
