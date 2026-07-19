"""Checkpoint tool handler tests — C2 (the founder judges via MCP).

The load-bearing proof is [R]: driving ``bsvibe_checkpoints_resolve`` on a real
paused run (a seeded RUNNING run + PENDING ``ask_user_question`` Decision, no
``dependency_overrides`` on the resolve path) actually RESOLVES the Decision,
folds the answer into ``run.payload['resolved_decisions']``, AND transitions the
run RUNNING → OPEN through the SAME C1 service the PWA drives — so the run is
re-drivable by ``AgentWorker.drive_once`` (which scans OPEN runs), not merely
flag-flipped (the E40 lesson).

Plus the ship-gate (``action_key='ship'`` ⇒ ToolError, Decision untouched), the
list shape (question + options + actions, ship filtered out), REST↔MCP parity,
and ``extra=forbid``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


def _ctx(session: AsyncSession, *, workspace_id, user_id, scopes) -> ToolContext:
    return ToolContext(
        principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=scopes),
        session=session,
    )


async def _seed_pending_question(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    question: str = "Which DB?",
    options: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a RUNNING run + a PENDING ``ask_user_question`` Decision on it."""
    payload: dict = {"question": question}
    if options is not None:
        payload["options"] = options
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=RunStatus.RUNNING,
        payload={"intent_text": "build the answer"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        decision="ask_user_question",
        payload=payload,
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.flush()
    return run.id, decision.id


async def _seed_verification_failed(
    session: AsyncSession,
    workspace_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a RUNNING run + a PENDING ``verification_failed`` executor Decision
    (the kind that carries the ship / retry / discard action set)."""
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=RunStatus.RUNNING,
        payload={"intent_text": "ship the feature"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        decision="verification_failed",
        payload={"reason": "tests did not pass", "artifact_refs": []},
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.flush()
    return run.id, decision.id


# ---------------------------------------------------------------------------
# [R] The load-bearing proof: MCP resolve ⇒ run resumes.
# ---------------------------------------------------------------------------
async def test_mcp_resolve_resolves_folds_and_resumes_run(
    db, workspace_id, user_id, registry
) -> None:
    async with db() as s:
        run_id, decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()

    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write"))
        out = await registry.call_tool(
            "bsvibe_checkpoints_resolve",
            {"checkpoint_id": str(decision_id), "answer": "Use Postgres"},
            ctx,
        )

    # (a) the outcome reports a resolved Decision, (c) run RUNNING → OPEN.
    assert out["id"] == str(decision_id)
    assert out["run_id"] == str(run_id)
    assert out["status"] == DecisionStatus.RESOLVED.value
    assert out["resolution"] == "Use Postgres"
    assert out["run_status"] == RunStatus.OPEN.value

    # The persisted state proves it went through the real resume path (the C1
    # service's AgentRunner.transition RUNNING → OPEN), not a flag flip.
    async with db() as s:
        decision = await s.get(Decision, decision_id)
        run = await s.get(ExecutionRun, run_id)
    assert decision is not None
    assert decision.status is DecisionStatus.RESOLVED
    assert decision.resolution == "Use Postgres"
    assert decision.resolved_by == user_id
    assert run is not None
    # (c) the run is re-drivable: OPEN is exactly what AgentWorker.drive_once scans.
    assert run.status is RunStatus.OPEN
    # (b) the answer is folded into the run payload for the loop to seed.
    assert run.payload["resolved_decisions"] == [
        {
            "decision_id": str(decision_id),
            "question": "Which DB?",
            "answer": "Use Postgres",
        }
    ]


# ---------------------------------------------------------------------------
# [Ship-gate] ship over MCP ⇒ ToolError, Decision + run untouched.
# ---------------------------------------------------------------------------
async def test_mcp_resolve_rejects_ship_action(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        run_id, decision_id = await _seed_verification_failed(s, workspace_id)
        await s.commit()

    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write"))
        with pytest.raises(ToolError, match="only in the PWA"):
            await registry.call_tool(
                "bsvibe_checkpoints_resolve",
                {"checkpoint_id": str(decision_id), "action_key": "ship"},
                ctx,
            )

    # The Decision stays pending and the run is unchanged — the gate fired
    # before any side effect.
    async with db() as s:
        decision = await s.get(Decision, decision_id)
        run = await s.get(ExecutionRun, run_id)
    assert decision is not None and decision.status is DecisionStatus.PENDING
    assert decision.resolution is None
    assert run is not None and run.status is RunStatus.RUNNING


async def test_mcp_resolve_allows_retry_action(db, workspace_id, user_id, registry) -> None:
    """The non-ship action (`retry`) IS accepted over MCP — it resumes the run
    RUNNING → OPEN (its C1 fall-through), so a failed run is recoverable."""
    async with db() as s:
        run_id, decision_id = await _seed_verification_failed(s, workspace_id)
        await s.commit()

    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write"))
        out = await registry.call_tool(
            "bsvibe_checkpoints_resolve",
            {"checkpoint_id": str(decision_id), "action_key": "retry"},
            ctx,
        )
    assert out["status"] == DecisionStatus.RESOLVED.value
    assert out["resolution"] == "retry"
    assert out["run_status"] == RunStatus.OPEN.value
    async with db() as s:
        run = await s.get(ExecutionRun, run_id)
    assert run is not None and run.status is RunStatus.OPEN


# ---------------------------------------------------------------------------
# [List] pending Decisions with question + options + actions (ship filtered).
# ---------------------------------------------------------------------------
async def test_mcp_list_pending_returns_question_options_and_actions(
    db, workspace_id, user_id, registry
) -> None:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", language="en"))
        _run_id, q_decision_id = await _seed_pending_question(
            s, workspace_id, question="Which DB?", options=["Postgres", "SQLite"]
        )
        _vf_run_id, vf_decision_id = await _seed_verification_failed(s, workspace_id)
        await s.commit()

    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",))
        out = await registry.call_tool("bsvibe_checkpoints_list_pending", {}, ctx)

    assert out["total"] == 2
    by_id = {item["id"]: item for item in out["items"]}

    q_item = by_id[str(q_decision_id)]
    assert q_item["kind"] == "ask_user_question"
    assert q_item["question"] == "Which DB?"
    assert q_item["options"] == ["Postgres", "SQLite"]
    # A vanilla ask_user_question has no one-click actions.
    assert q_item["actions"] is None

    vf_item = by_id[str(vf_decision_id)]
    assert vf_item["kind"] == "verification_failed"
    action_keys = [a["key"] for a in vf_item["actions"]]
    # The ship-gate is reflected in the listed actions: retry + discard, NEVER ship.
    assert action_keys == ["retry", "discard"]
    assert "ship" not in action_keys


# ---------------------------------------------------------------------------
# [Parity] MCP resolve outcome == REST resolve outcome for the same input.
# ---------------------------------------------------------------------------
async def test_mcp_and_rest_resolve_produce_identical_outcomes(
    db, workspace_id, user_id, registry
) -> None:
    async with db() as s:
        rest_run_id, rest_decision_id = await _seed_pending_question(s, workspace_id)
        mcp_run_id, mcp_decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()

    # REST path (C1 service via the endpoint).
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_current_user_row] = lambda: SimpleNamespace(id=user_id)
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            f"/api/v1/checkpoints/{rest_decision_id}/resolve",
            json={"answer": "Use Postgres"},
        )
    assert r.status_code == 200, r.text
    rest_body = r.json()

    # MCP path (C1 service via the tool).
    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write"))
        mcp_out = await registry.call_tool(
            "bsvibe_checkpoints_resolve",
            {"checkpoint_id": str(mcp_decision_id), "answer": "Use Postgres"},
            ctx,
        )

    # Identical wire-relevant outcome (modulo the distinct ids).
    assert rest_body["status"] == mcp_out["status"] == DecisionStatus.RESOLVED.value
    assert rest_body["resolution"] == mcp_out["resolution"] == "Use Postgres"
    assert rest_body["run_status"] == mcp_out["run_status"] == RunStatus.OPEN.value

    async with db() as s:
        rest_run = await s.get(ExecutionRun, rest_run_id)
        mcp_run = await s.get(ExecutionRun, mcp_run_id)
    assert rest_run is not None and mcp_run is not None
    rest_fold = rest_run.payload["resolved_decisions"]
    mcp_fold = mcp_run.payload["resolved_decisions"]
    assert len(rest_fold) == len(mcp_fold) == 1
    assert rest_fold[0]["answer"] == mcp_fold[0]["answer"] == "Use Postgres"
    assert rest_fold[0]["question"] == mcp_fold[0]["question"] == "Which DB?"


# ---------------------------------------------------------------------------
# Guards — not-found, scopes, extra=forbid.
# ---------------------------------------------------------------------------
async def test_mcp_resolve_unknown_checkpoint_raises(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write"))
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool(
                "bsvibe_checkpoints_resolve",
                {"checkpoint_id": str(uuid.uuid4()), "answer": "x"},
                ctx,
            )


async def test_mcp_resolve_requires_write_scope(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        _run_id, decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()
    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",))
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_checkpoints_resolve",
                {"checkpoint_id": str(decision_id), "answer": "x"},
                ctx,
            )


async def test_mcp_resolve_rejects_unknown_field(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        _run_id, decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()
    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write"))
        with pytest.raises(ToolError, match="invalid arguments"):
            await registry.call_tool(
                "bsvibe_checkpoints_resolve",
                {"checkpoint_id": str(decision_id), "answer": "x", "bogus": 1},
                ctx,
            )


async def test_mcp_list_pending_requires_read_scope(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = _ctx(s, workspace_id=workspace_id, user_id=user_id, scopes=())
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_checkpoints_list_pending", {}, ctx)
