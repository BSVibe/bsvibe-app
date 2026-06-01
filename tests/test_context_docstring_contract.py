"""Lift N-Coverage pattern #8 — each bounded context's ``__init__.py`` MUST
carry a contract docstring that states:

1. What the context owns (a single sentence containing the word "owns").
2. What is NOT exposed (a single sentence containing "private" or "internals").

v8 §3 enumerates the 6 bounded contexts. ``api/`` is the HTTP surface and
also receives the contract — it is the public entry seam, so the contract
clarifies what it owns and what should remain behind the contexts.

The check is structural — read the module docstring, then assert the two
keyword markers are present. Phase 1 (informational) — Phase 2 may
extend this to grep for facade references and exact wording.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The 6 contexts in v8 §3 — plus the HTTP surface (api/) since it is the
# externally visible seam and benefits from the same contract.
CONTEXTS_REQUIRING_CONTRACT: tuple[str, ...] = (
    "api",
    "router",
    "knowledge",
    "workflow",
    "identity",
    "schedule",
    "extensions",
)

# Keywords the docstring must contain (case-insensitive, anywhere).
OWNS_KEYWORDS: tuple[str, ...] = ("owns", "owns:")
PRIVACY_KEYWORDS: tuple[str, ...] = (
    "private",
    "internals",
    "not exposed",
    "not part of the public",
)


def _module_docstring(init_path: Path) -> str | None:
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    return ast.get_docstring(tree)


@pytest.mark.parametrize("context", CONTEXTS_REQUIRING_CONTRACT)
def test_context_init_has_contract_docstring(context: str) -> None:
    init = REPO_ROOT / "backend" / context / "__init__.py"
    assert init.exists(), f"context {context!r} missing __init__.py"

    docstring = _module_docstring(init)
    assert docstring, (
        f"context {context!r} __init__.py has no module docstring — "
        "Lift N-Coverage pattern #8 requires a context contract docstring."
    )

    lower = docstring.lower()
    assert any(k in lower for k in OWNS_KEYWORDS), (
        f"context {context!r} docstring missing an 'owns' statement — "
        "say in one sentence what this context owns."
    )
    assert any(k in lower for k in PRIVACY_KEYWORDS), (
        f"context {context!r} docstring missing a privacy statement — "
        "say in one sentence what is NOT exposed (e.g., 'internals at "
        "domain/application/infrastructure are private; only "
        "application/__init__.py exports are public')."
    )
