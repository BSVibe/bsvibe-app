"""The inspector's footer trimmer must match what the SettleWorker actually emits.

``_helpers._SETTLE_FOOTER_PREFIXES`` is a hardcoded copy of the footer shape that
``settle_worker._observation_body`` produces. Nothing tied the two together: the
only link was a comment — and that comment pointed at
``backend/workers/settle_worker.py``, a path that has not existed since the module
moved under ``backend/knowledge/infrastructure/workers/``. So a reader following
the coupling found nothing, and a drift in the producer would silently stop the
trimming: the founder would just start seeing ``Product:`` / ``Run: <uuid>``
metadata in the inspector, with every test still green.

This runs the real producer into the real trimmer. Fixing the stale path in the
comment was the small half of this; pinning the coupling it described is the
point.
"""

from __future__ import annotations

import uuid

from backend.api.v1.inside._helpers import _strip_settle_footer
from backend.knowledge.infrastructure.workers.settle_worker import (
    Settlement,
    _observation_body,
)

_NARRATIVE = "에이전트가 라우팅 버그를 고쳤어요.\n두 번째 문단도 남습니다."


def _settlement(**overrides: object) -> Settlement:
    base: dict[str, object] = {
        "workspace_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "activity_id": uuid.uuid4(),
        "verified": True,
        "summary": _NARRATIVE,
        "artifact_refs": ["backend/router/dispatch.py", "tests/test_dispatch.py"],
        "product_slug": "bsvibe",
        "intent_text": "라우팅이 이상해요",
    }
    base.update(overrides)
    return Settlement(**base)  # type: ignore[arg-type]


def test_the_trimmer_removes_every_footer_line_the_producer_emits() -> None:
    """The load-bearing assertion: narrative survives, metadata is all gone."""
    body = _observation_body(_settlement(), _NARRATIVE)

    kept = "\n".join(_strip_settle_footer(body.splitlines())).strip()

    assert kept == _NARRATIVE
    for marker in ("Product:", "Intent:", "## Artifacts", "Verified:", "Run:"):
        assert marker not in kept


def test_artifact_bullets_are_swept_by_their_own_marker() -> None:
    """The artifact BULLETS are not in the prefix tuple — they survive only
    because the trimmer cuts from the first marker to the end.

    This needs a settlement with NO product/intent: with them present the cut
    happens at ``Product:`` regardless, so the same assertion would pass however
    the bullets were ordered — measuring nothing. (Checked by cutting the wire:
    moving the bullets above ``## Artifacts`` left the richer case green.)
    """
    body = _observation_body(
        _settlement(product_slug=None, product_name=None, intent_text=None),
        _NARRATIVE,
    )

    kept = "\n".join(_strip_settle_footer(body.splitlines())).strip()

    assert kept == _NARRATIVE
    assert "backend/router/dispatch.py" not in kept


def test_it_holds_when_the_optional_context_is_absent() -> None:
    """Connector-inbound runs carry no product/intent — the footer shrinks, and
    the trimmer must still cut at the first marker that IS present."""
    body = _observation_body(
        _settlement(product_slug=None, product_name=None, intent_text=None, artifact_refs=[]),
        _NARRATIVE,
    )

    kept = "\n".join(_strip_settle_footer(body.splitlines())).strip()

    assert kept == _NARRATIVE


def test_a_narrative_with_no_footer_is_left_alone() -> None:
    """Negative control — the trimmer must be able to keep everything, or the
    test above could pass by cutting indiscriminately."""
    lines = _NARRATIVE.splitlines()

    assert _strip_settle_footer(lines) == lines
