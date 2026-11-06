"""A Safe Mode card carries the independent review of its run's LAST PASSED round.

Real run + ``VerificationResult`` rows + queue rows, read through the real MCP tool and the
real REST routes.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.delivery.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine, fake_current_user, publishing_workspace
from ._rls import scoped_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("dispatch_publishes_workspace")]

P, F = VerificationOutcome.PASSED, VerificationOutcome.FAILED


def _finding(severity: str, n: int) -> dict[str, Any]:
    return {
        "severity": severity,
        "file": f"src/f{n}.py",
        "line": n,
        "claim": f"{severity} claim {n}",
        "failure_scenario": f"{severity} scenario {n}",
    }


def _done(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"review": {"status": "done", "findings": list(findings)}}


UNAVAILABLE = {"review": {"status": "unavailable", "reason": "timed out"}}


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def client(db, workspace_id):
    app = create_app()
    user = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")

    async def _session():
        async with db() as s:
            yield s

    async def _user_row():
        return user

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _hold(
    db, workspace_id: uuid.UUID, rounds: list[tuple[VerificationOutcome, dict[str, Any]]]
) -> uuid.UUID:
    """A run with ``rounds`` stored as VerificationResults (oldest first), holding one item."""
    async with scoped_session(db, workspace_id) as s:
        if await s.get(WorkspaceRow, workspace_id) is None:
            s.add(WorkspaceRow(id=workspace_id, name="ws"))
            await s.flush()
        base = datetime.now(UTC) - timedelta(hours=1)
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            status=RunStatus.RUNNING,
            payload={},
            created_at=base,
            updated_at=base,
        )
        s.add(run)
        await s.flush()
        for i, (outcome, result) in enumerate(rounds):
            s.add(
                VerificationResult(
                    run_id=run.id,
                    workspace_id=workspace_id,
                    outcome=outcome,
                    contract={},
                    result=result,
                    created_at=base + timedelta(minutes=i),
                )
            )
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            deliverable_type=DeliverableType.DIRECT_OUTPUT,
            payload={},
        )
        s.add(deliverable)
        await s.flush()
        await SafeModeQueue(s).enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable.id, run_id=run.id
        )
        await s.commit()
        return deliverable.id


async def _cards(db, registry, client, workspace_id, deliverable_id) -> list[dict[str, Any]]:
    async with db() as s:
        principal = McpPrincipal(
            user_id=uuid.uuid4(),
            workspace_id=workspace_id,
            client_id="dcr-test",
            scopes=frozenset({"mcp:read"}),
            jti=uuid.uuid4(),
        )
        pending = await registry.call_tool(
            "bsvibe_safe_mode_list_pending", {}, ToolContext(principal=principal, session=s)
        )
    pick = lambda items: next(i for i in items if i["deliverable_id"] == str(deliverable_id))  # noqa: E731
    flat = (await client.get("/api/v1/safemode/queue")).json()
    grouped = (await client.get("/api/v1/safemode/queue/by-run")).json()
    return [
        pick(pending["items"]),
        pick(flat),
        pick([i for g in grouped for i in g["items"]]),
    ]


async def test_card_carries_the_last_passed_rounds_review(db, registry, client, workspace_id):
    older = _done(_finding("high", 1))
    last = _done(
        _finding("low", 1),
        _finding("high", 2),
        _finding("medium", 3),
        _finding("low", 4),
        _finding("medium", 5),
        _finding("low", 6),
        _finding("high", 7),
    )
    # A later FAILED round's review must not be picked: the card shows the last PASS.
    deliverable_id = await _hold(
        db, workspace_id, [(P, older), (P, last), (F, _done(_finding("high", 9)))]
    )

    for card in await _cards(db, registry, client, workspace_id, deliverable_id):
        assert card["review_status"] == "done"
        assert card["review_counts"] == {"high": 2, "medium": 2, "low": 3}
        assert card["review_findings"] == [
            {"severity": "high", "file": "src/f2.py", "line": 2, "claim": "high claim 2"},
            {"severity": "high", "file": "src/f7.py", "line": 7, "claim": "high claim 7"},
            {"severity": "medium", "file": "src/f3.py", "line": 3, "claim": "medium claim 3"},
            {"severity": "medium", "file": "src/f5.py", "line": 5, "claim": "medium claim 5"},
            {"severity": "low", "file": "src/f1.py", "line": 1, "claim": "low claim 1"},
        ]


async def test_unavailable_and_missing_reviews(db, registry, client, workspace_id):
    unavailable = await _hold(db, workspace_id, [(P, {}), (P, UNAVAILABLE)])
    none = await _hold(db, workspace_id, [(P, {"requirements": []})])
    never_passed = await _hold(db, workspace_id, [(F, _done(_finding("high", 1)))])

    for card in await _cards(db, registry, client, workspace_id, unavailable):
        assert (card["review_status"], card["review_counts"], card["review_findings"]) == (
            "unavailable",
            {},
            [],
        )
    for deliverable_id in (none, never_passed):
        for card in await _cards(db, registry, client, workspace_id, deliverable_id):
            assert (card["review_status"], card["review_counts"], card["review_findings"]) == (
                "none",
                {},
                [],
            )


async def test_a_clean_review_is_done_with_zero_counts(db, registry, client, workspace_id):
    deliverable_id = await _hold(db, workspace_id, [(P, _done())])

    for card in await _cards(db, registry, client, workspace_id, deliverable_id):
        assert card["review_status"] == "done"
        assert card["review_counts"] == {"high": 0, "medium": 0, "low": 0}
        assert card["review_findings"] == []
