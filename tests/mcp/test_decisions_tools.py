"""Decision tool handler tests — Lift D3b.

Injects fake :class:`InMemoryCanonicalizationIndex` / :class:`CanonicalizationService`
into ``ctx.extras`` so the unit run never touches the on-disk vault. The
behaviour under test is the tool wrapper (input → service call → output
shape) — the real index + service are covered by their own suites under
``tests/knowledge/canonicalization``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.knowledge.canonicalization import models
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


def _proposal(
    *,
    path: str,
    kind: str = "merge-concepts",
    status: str = "pending",
    action_drafts: list[str] | None = None,
) -> models.ProposalEntry:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
    return models.ProposalEntry(
        path=path,
        kind=kind,
        status=status,
        strategy="alias-merge",
        generator="canon-test",
        generator_version="1.0.0",
        proposal_score=0.8,
        created_at=now,
        updated_at=now,
        expires_at=now,
        action_drafts=action_drafts or ["actions/merge/2026-06-05T12-00-00Z__merge.md"],
    )


def _decision(
    *,
    path: str,
    kind: str = "must-link",
    status: str = "active",
) -> models.DecisionEntry:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
    return models.DecisionEntry(
        path=path,
        kind=kind,
        status=status,
        maturity="confirmed",
        decision_schema_version="1.0",
        subjects=("a", "b"),
        base_confidence=1.0,
        last_confirmed_at=now,
        decay_profile="definitional",
        decay_halflife_days=None,
        valid_from=now,
        created_at=now,
        updated_at=now,
    )


class _FakeIndex:
    """Duck-typed stand-in for InMemoryCanonicalizationIndex."""

    def __init__(
        self,
        proposals: list[models.ProposalEntry] | None = None,
        decisions: list[models.DecisionEntry] | None = None,
    ) -> None:
        self._proposals = proposals or []
        self._decisions = decisions or []

    async def list_proposals(
        self, *, status: str | None = None, kind: str | None = None
    ) -> list[models.ProposalEntry]:
        out = list(self._proposals)
        if status is not None:
            out = [p for p in out if p.status == status]
        return out

    async def list_decisions(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[models.DecisionEntry]:
        return list(self._decisions)


class _FakeService:
    """Duck-typed stand-in for CanonicalizationService accept/reject."""

    def __init__(self, *, accept_results: list[Any] | None = None) -> None:
        self.accept_results = accept_results or []
        self.accept_calls: list[tuple[str, str]] = []
        self.reject_calls: list[tuple[str, str, str | None]] = []
        self.raise_on_accept: Exception | None = None
        self.raise_on_reject: Exception | None = None

    async def accept_proposal(self, path: str, *, actor: str) -> list[Any]:
        if self.raise_on_accept is not None:
            raise self.raise_on_accept
        self.accept_calls.append((path, actor))
        return list(self.accept_results)

    async def reject_proposal(self, path: str, *, actor: str, reason: str | None = None) -> None:
        if self.raise_on_reject is not None:
            raise self.raise_on_reject
        self.reject_calls.append((path, actor, reason))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
_VALID_PROPOSAL_PATH = "proposals/merge-concepts/2026-06-05T12-00-00Z__p.md"


async def test_list_returns_proposals_in_descending_order(
    db, workspace_id, user_id, registry, seeded
) -> None:
    p1 = _proposal(path=_VALID_PROPOSAL_PATH)
    p2 = _proposal(path="proposals/merge-concepts/2026-06-06T12-00-00Z__p.md")
    # Force p2 newer
    p2.created_at = datetime(2026, 6, 7, tzinfo=UTC)
    fake = _FakeIndex(proposals=[p1, p2])
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"canon_index": fake},
        )
        out = await registry.call_tool("bsvibe_decisions_list", {"limit": 50}, ctx)
    assert [r["id"] for r in out] == [p2.path, p1.path]
    # Action handle was derived from the first draft
    assert out[0]["action_kind"] == "merge"


async def test_list_status_filter_passthrough(db, workspace_id, user_id, registry, seeded) -> None:
    p_pending = _proposal(path=_VALID_PROPOSAL_PATH, status="pending")
    p_accepted = _proposal(
        path="proposals/merge-concepts/2026-06-04T12-00-00Z__p.md",
        status="accepted",
    )
    fake = _FakeIndex(proposals=[p_pending, p_accepted])
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"canon_index": fake},
        )
        out = await registry.call_tool("bsvibe_decisions_list", {"status": "pending"}, ctx)
    assert {r["status"] for r in out} == {"pending"}


async def test_show_returns_full_proposal(db, workspace_id, user_id, registry, seeded) -> None:
    p = _proposal(path=_VALID_PROPOSAL_PATH)
    fake = _FakeIndex(proposals=[p])
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"canon_index": fake},
        )
        out = await registry.call_tool(
            "bsvibe_decisions_show", {"decision_id": _VALID_PROPOSAL_PATH}, ctx
        )
    assert out["id"] == _VALID_PROPOSAL_PATH
    assert out["generator"] == "canon-test"


async def test_show_rejects_non_proposal_path(db, workspace_id, user_id, registry, seeded) -> None:
    fake = _FakeIndex()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"canon_index": fake},
        )
        with pytest.raises(ToolError, match="proposal not found"):
            await registry.call_tool(
                "bsvibe_decisions_show",
                {"decision_id": "not/a/proposal/path.md"},
                ctx,
            )


async def test_log_returns_decisions(db, workspace_id, user_id, registry, seeded) -> None:
    d = _decision(path="decisions/must-link/2026-06-05T12-00-00Z__d.md")
    fake = _FakeIndex(decisions=[d])
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"canon_index": fake},
        )
        out = await registry.call_tool("bsvibe_decisions_log", {}, ctx)
    assert len(out) == 1
    assert out[0]["decision_kind"] == "must-link"


async def test_resolve_accept_calls_service(db, workspace_id, user_id, registry, seeded) -> None:
    result = models.ApplyResult(
        action_path="actions/merge/2026-06-05T12-00-00Z__merge.md",
        final_status="applied",
        affected_paths=["concepts/active/foo.md"],
    )
    svc = _FakeService(accept_results=[result])
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"canon_service": svc},
        )
        out = await registry.call_tool(
            "bsvibe_decisions_resolve",
            {"decision_id": _VALID_PROPOSAL_PATH, "action": "accept"},
            ctx,
        )
    assert out["status"] == "accepted"
    assert svc.accept_calls == [(_VALID_PROPOSAL_PATH, str(user_id))]


async def test_resolve_reject_records_reason(db, workspace_id, user_id, registry, seeded) -> None:
    svc = _FakeService()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"canon_service": svc},
        )
        out = await registry.call_tool(
            "bsvibe_decisions_resolve",
            {
                "decision_id": _VALID_PROPOSAL_PATH,
                "action": "reject",
                "comment": "duplicate",
            },
            ctx,
        )
    assert out["status"] == "rejected"
    assert svc.reject_calls == [(_VALID_PROPOSAL_PATH, str(user_id), "duplicate")]


async def test_resolve_rejects_invalid_action(db, workspace_id, user_id, registry, seeded) -> None:
    svc = _FakeService()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"canon_service": svc},
        )
        with pytest.raises(ToolError, match="action must be"):
            await registry.call_tool(
                "bsvibe_decisions_resolve",
                {"decision_id": _VALID_PROPOSAL_PATH, "action": "maybe"},
                ctx,
            )


async def test_resolve_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"canon_service": _FakeService()},
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_decisions_resolve",
                {"decision_id": _VALID_PROPOSAL_PATH, "action": "accept"},
                ctx,
            )


async def test_resolve_accept_translates_missing_proposal(
    db, workspace_id, user_id, registry, seeded
) -> None:
    svc = _FakeService()
    svc.raise_on_accept = FileNotFoundError("gone")
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"canon_service": svc},
        )
        with pytest.raises(ToolError, match="proposal not found"):
            await registry.call_tool(
                "bsvibe_decisions_resolve",
                {"decision_id": _VALID_PROPOSAL_PATH, "action": "accept"},
                ctx,
            )
