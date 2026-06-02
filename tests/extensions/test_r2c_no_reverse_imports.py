"""Lift Q3 / R2c — assert no reverse-direction ``plugin.*`` imports remain.

The architectural invariant: the dependency direction is

    plugin/<name>/  →  bsvibe_sdk  →  backend.extensions  →  backend.*

— never backend → plugin. The pre-Q3 ``backend.connectors.resolver`` and
``backend.api.webhooks`` modules violated this by importing each plugin's
local ``webhook`` module directly to get the parser callable + the
signature-error class. After Lift Q3 those reverse imports must be gone:
parsers live in :class:`WebhookParserRegistry`, the signature-error class
lives at :class:`bsvibe_sdk.WebhookSignatureError`.

The ``plugin.audit`` in-tree subscriber is intentionally NOT covered by
this gate — it is an in-tree transactional-outbox subscriber, not a
connector, and is whitelisted in the import-linter contract. This test
guards only the two surfaces R2c flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Two surfaces R2c flagged. Each must end up with zero ``plugin.*`` imports
# (we do NOT exclude ``plugin.audit`` from these specific files because
# neither file historically imported plugin.audit; if a future change
# leaks one in, this test surfaces it).
_GUARDED_FILES = [
    _REPO_ROOT / "backend" / "connectors" / "resolver.py",
    _REPO_ROOT / "backend" / "api" / "webhooks.py",
]

# Match both ``from plugin.<x> import ...`` and ``import plugin.<x>``.
_PLUGIN_IMPORT_PATTERN = re.compile(r"^\s*(?:from\s+plugin\.|import\s+plugin\.)")


@pytest.mark.parametrize("path", _GUARDED_FILES, ids=lambda p: p.name)
def test_no_reverse_plugin_imports(path: Path) -> None:
    """Source file must contain zero ``plugin.*`` imports (R2c invariant)."""
    assert path.exists(), f"guarded file missing: {path}"
    offenders: list[tuple[int, str]] = []
    for idx, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # Strip end-of-line comments to dodge the ``# from plugin.foo …``
        # case in commentary that is not an actual import.
        code = raw_line.split("#", 1)[0]
        if _PLUGIN_IMPORT_PATTERN.match(code):
            offenders.append((idx, raw_line.strip()))
    assert not offenders, (
        f"R2c violation in {path.name}: reverse-direction plugin imports found:\n"
        + "\n".join(f"  L{n}: {line}" for n, line in offenders)
    )


def test_resolver_imports_through_engine_registry() -> None:
    """The resolver depends on the engine's WebhookParserRegistry, not plugins."""
    text = _GUARDED_FILES[0].read_text(encoding="utf-8")
    assert "WebhookParserRegistry" in text, (
        "backend.connectors.resolver must dispatch through the engine registry"
    )


def test_webhook_route_uses_sdk_signature_error() -> None:
    """The HTTP route catches the SDK base, not per-plugin error subclasses."""
    text = _GUARDED_FILES[1].read_text(encoding="utf-8")
    assert "from bsvibe_sdk import WebhookSignatureError" in text, (
        "backend.api.webhooks must catch the SDK WebhookSignatureError, "
        "not import per-plugin error subclasses"
    )
