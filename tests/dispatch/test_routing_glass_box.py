"""Routing stays glass-box — the founder can see WHICH model ran a step and WHY.

Drift audit §C. The founder's four routing rules work (the new dispatch path
reads them), but the OLD path's ``activity_type="routing_decision"`` record —
written *"so routing stays glass-box"* — never moved to
``dispatch/resolver.py``. prod: **0 rows**, and 18% of runs (25/139) carry a
stage that routes them to a NON-default model. Which one, the founder cannot see.

Every test here asserts **both ends of the seam**: the row is written AND
``_activity_label`` turns it into a line the timeline actually renders. A
producer alone is worth nothing — ``_build_timeline`` drops any activity type
whose label is ``None``, silently. That is the failure this audit item is about.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Registers ExecutionRun / ExecutionRunActivity on the shared Base.metadata.
import backend.workflow.infrastructure.db  # noqa: F401
from backend.api.v1.runs._helpers import _activity_label, _build_timeline
from backend.config import get_settings
from backend.dispatch.caller_registry import CALLER_FRAME
from backend.dispatch.resolver import ModelAccountResolver
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus

pytestmark = pytest.mark.asyncio

ROUTING_DECISION = "routing_decision"


@pytest_asyncio.fixture
async def run(session: AsyncSession, workspace: WorkspaceRow) -> AsyncIterator[ExecutionRun]:
    row = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        status=RunStatus.RUNNING,
        payload={"intent_text": "add a cache"},
    )
    session.add(row)
    await session.flush()
    yield row


async def _routing_rows(session: AsyncSession) -> list[ExecutionRunActivity]:
    stmt = select(ExecutionRunActivity).where(
        ExecutionRunActivity.activity_type == ROUTING_DECISION
    )
    return list((await session.execute(stmt)).scalars().all())


async def test_a_resolved_route_becomes_a_line_the_founder_can_read(
    session: AsyncSession,
    workspace: WorkspaceRow,
    model_account: ModelAccount,
    cloud_account: ModelAccount,
    run: ExecutionRun,
) -> None:
    """The whole point: resolving a route leaves a record AND that record
    renders. Asserting only the row would pass while the timeline stays blank."""
    rule = RunRoutingRuleRow(
        workspace_id=workspace.id,
        name="frame -> cloud",
        caller_id=CALLER_FRAME,
        priority=10,
        is_default=False,
        target=cloud_account.litellm_model,
        conditions=[],
        is_active=True,
    )
    session.add(rule)
    workspace.default_account_id = model_account.id
    await session.flush()

    resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
    await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)

    rows = await _routing_rows(session)
    assert len(rows) == 1
    assert rows[0].run_id == run.id
    assert rows[0].workspace_id == workspace.id
    assert rows[0].payload["source"] == "explicit_rule"
    assert rows[0].payload["target"] == cloud_account.litellm_model

    # The consumer end — without this the row is invisible.
    label = _activity_label(rows[0].activity_type, rows[0].payload)
    assert label is not None, "routing_decision has no label — the timeline drops it"
    assert cloud_account.litellm_model in label


async def test_the_same_route_is_recorded_once_however_many_calls(
    session: AsyncSession,
    workspace: WorkspaceRow,
    model_account: ModelAccount,
    run: ExecutionRun,
) -> None:
    """``resolve_for`` runs per LLM call — prod runs reach 24 turns. Without
    dedup the run's STORY timeline floods with one identical line per call."""
    workspace.default_account_id = model_account.id
    await session.flush()

    for _ in range(5):
        resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
        await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)

    assert len(await _routing_rows(session)) == 1


async def test_a_genuinely_different_route_gets_its_own_line(
    session: AsyncSession,
    workspace: WorkspaceRow,
    model_account: ModelAccount,
    cloud_account: ModelAccount,
    run: ExecutionRun,
) -> None:
    """Dedup must not collapse real changes: the founder's design/impl split
    routes one run to two different models, and BOTH are the glass-box answer."""
    rule = RunRoutingRuleRow(
        workspace_id=workspace.id,
        name="frame -> cloud",
        caller_id=CALLER_FRAME,
        priority=10,
        is_default=False,
        target=cloud_account.litellm_model,
        conditions=[],
        is_active=True,
    )
    session.add(rule)
    workspace.default_account_id = model_account.id
    await session.flush()

    resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
    await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
    # A different caller falls through to the workspace default — a different
    # (caller, source, target) triple, so a different line.
    resolver2 = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
    await resolver2.resolve_for(caller_id="workflow.judge", workspace_id=workspace.id)

    rows = await _routing_rows(session)
    assert len(rows) == 2
    assert {r.payload["source"] for r in rows} == {"explicit_rule", "workspace_default"}


async def test_the_workspace_default_says_so_in_words(
    session: AsyncSession,
    workspace: WorkspaceRow,
    model_account: ModelAccount,
    run: ExecutionRun,
) -> None:
    """ "Why this model" has two honest answers — your rule, or the default.
    The line must distinguish them; otherwise it explains nothing."""
    workspace.default_account_id = model_account.id
    await session.flush()

    resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
    await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)

    rows = await _routing_rows(session)
    assert len(rows) == 1
    label = _activity_label(rows[0].activity_type, rows[0].payload)
    assert label is not None
    assert "workspace default" in label.lower()
    assert "your routing rule" not in label.lower()


async def test_a_call_outside_a_run_records_nothing(
    session: AsyncSession,
    workspace: WorkspaceRow,
    model_account: ModelAccount,
) -> None:
    """chat / rule-compile call the resolver with no run. There is no timeline
    to write to, and inventing a run_id would corrupt another run's story."""
    workspace.default_account_id = model_account.id
    await session.flush()

    resolver = ModelAccountResolver(session, settings=get_settings())
    await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)

    assert await _routing_rows(session) == []


async def test_the_line_survives_the_actual_timeline_builder(
    session: AsyncSession,
    workspace: WorkspaceRow,
    model_account: ModelAccount,
    run: ExecutionRun,
) -> None:
    """The end the founder actually reads.

    ``_activity_label`` returning a string is NECESSARY but not SUFFICIENT —
    ``_build_timeline`` is what silently drops events (``label is None`` →
    ``continue``), and it is the function the run-detail endpoint calls. Assert
    the row survives THAT, or this whole lift is another write-only signal.
    """
    workspace.default_account_id = model_account.id
    await session.flush()

    resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
    await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)

    rows = await _routing_rows(session)
    events, source = _build_timeline(rows, None, None, None)

    assert source == "activities"
    assert [e.type for e in events] == [ROUTING_DECISION]
    assert model_account.litellm_model in events[0].label


async def test_a_malformed_row_degrades_to_a_drop_not_a_500(
    session: AsyncSession,
) -> None:
    """Every other label reader is defensive; this one must be too — the run
    detail endpoint renders through a response model that 500s on a bad type."""
    assert _activity_label(ROUTING_DECISION, {}) is None
    assert _activity_label(ROUTING_DECISION, {"target": 123, "source": None}) is None
