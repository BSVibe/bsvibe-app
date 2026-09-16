"""Every ``Settings`` field must be read by code, not merely declared.

An inert setting is worse than no setting: it presents itself as a knob, so an
operator turns it to change behaviour and **nothing happens — no error, no way to
find out why**. Measured here, all five that failed this guard sat in one block
whose comment reads *"Operator may tune for local-LLM vs frontier-model
deployments; defaults match Cycle 7-14 dogfood telemetry"* — prose that describes
tuned, measured configuration — directly beneath ``execution_work_round_budget``,
which IS live (``agent_loop.py``). A reader has no way to tell the live sibling
from the dead ones.

Scope note — this guard deliberately checks ``backend/`` only, and counts a
reference ANYWHERE in it, including a quoted field name. Several live settings
are reached dynamically (``getattr(settings, "product_bundle_backend")``,
``getattr(settings, id_attr)`` off a literal tuple), so an attribute-access-only
check would call them dead. Within ``config.py`` the field's own declaration line
and every comment line are stripped first: a field must be READ, not merely
described by the prose sitting next to it.

Measured while writing this: 76 fields, 5 failures, **no allowlist needed**.
"""

from __future__ import annotations

import pathlib
import re

from backend.config import Settings

_BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
_CONFIG = _BACKEND / "config.py"


def _is_read(field: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(field)}\b")
    decl = re.compile(rf"\s*{re.escape(field)}\s*:")
    for path in _BACKEND.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path == _CONFIG:
            text = "\n".join(
                line
                for line in text.splitlines()
                if not line.lstrip().startswith("#") and not decl.match(line)
            )
        if pattern.search(text):
            return True
    return False


def test_no_settings_field_is_inert() -> None:
    """The load-bearing guard, pinned on the RESULT SET.

    Naming the five that were dead would only prove I can spell them; this fails
    for ANY future field that ships as an unread knob.
    """
    inert = sorted(f for f in Settings.model_fields if not _is_read(f))
    assert inert == [], inert


def test_the_guard_can_see_a_dynamically_read_field() -> None:
    """Positive control — without this, the guard above could be passing because
    the scan matches everything, and a real inert field would slip through.

    ``product_bundle_backend`` is never written as ``settings.product_bundle_backend``;
    it is reached as ``getattr(settings, "product_bundle_backend", ...)``. An
    attribute-access-only implementation would wrongly call it dead.
    """
    assert "product_bundle_backend" in Settings.model_fields
    assert _is_read("product_bundle_backend")


def test_the_guard_reports_a_field_that_nothing_reads() -> None:
    """Negative control — the guard must be able to say NO.

    A check that can only return "fine" measures nothing, so prove the predicate
    flips on a name that is declared nowhere and read nowhere.
    """
    assert not _is_read("execution_totally_imaginary_budget")
