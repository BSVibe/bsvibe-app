"""One failing delivery target must not take the others down with it.

Live 2026-08-10 (BStockReport M5 approval): the github call sits OUTSIDE the
per-binding ``try``, so when ``deliver_github`` raised ``GitError`` the exception
escaped ``dispatch()`` entirely — the telegram binding below it was never
reached, and the founder got nothing at all. The per-binding loop already had
this property ("a single bad target does not wedge the queue"); github was the
one target that did not.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Boom(RuntimeError):
    pass


async def test_a_raising_github_target_does_not_abort_the_other_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.workflow.application.delivery.connector_dispatch import _github_actions

    async def _raises(**_kw: Any) -> list[Any]:
        raise _Boom("git add -A failed: fatal: not a git repository")

    monkeypatch.setattr(
        "backend.workflow.application.delivery.connector_dispatch.deliver_github", _raises
    )

    actions = await _github_actions(
        deps=object(),
        binding=object(),
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        run_id=None,
        content={},
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.succeeded is False, "the failure is RECORDED, not swallowed silently"
    assert "not a git repository" in (action.error or "")


async def test_a_working_github_target_is_passed_through_unchanged() -> None:
    from backend.workflow.application.delivery.connector_dispatch import _github_actions

    sentinel = [object(), object()]

    async def _ok(**_kw: Any) -> list[Any]:
        return sentinel

    import backend.workflow.application.delivery.connector_dispatch as cd

    original = cd.deliver_github
    cd.deliver_github = _ok  # type: ignore[assignment]
    try:
        actions = await _github_actions(
            deps=object(),
            binding=object(),
            workspace_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            run_id=None,
            content={},
        )
    finally:
        cd.deliver_github = original  # type: ignore[assignment]

    assert actions == sentinel
