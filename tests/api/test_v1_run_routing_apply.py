"""/api/v1/run-routing/compile/apply — persist accepted proposals (Lift N3).

Apply is atomic: for a category proposal it creates the intent definition + the
``classified_intent`` rule; for a caller/condition proposal a plain rule; for the
default proposal it sets the workspace ``default_account_id``. Any failure rolls
back the whole batch. The embedder is stubbed — no real embedding API is hit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register every table the apply path touches on the shared Base.metadata.
import backend.embedding.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
import backend.router.accounts.models  # noqa: F401
import backend.router.routing.run_routing.db  # noqa: F401
from backend.api.v1.run_routing import ApplyError, ApplyProposal, apply_proposals
from backend.embedding.db import IntentDefinitionRow
from backend.embedding.service import EmbeddedExample
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.db import RunRoutingRuleRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio


class _StubEmbedder:
    """Deterministic embedder — never touches a real embedding API."""

    model = "stub-embed"

    async def embed_one(self, text: str) -> EmbeddedExample:
        return EmbeddedExample(text=text, embedding=[0.1, 0.2, 0.3], model=self.model)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


def _acct(ws: uuid.UUID, litellm_model: str) -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=ws,
        account_id=uuid.uuid4(),
        provider="executor",
        label=f"dogfood ({litellm_model})",
        litellm_model=litellm_model,
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"executor_type": "claude_code", "worker_id": str(uuid.uuid4())},
    )


@pytest_asyncio.fixture
async def seeded(maker, workspace_id) -> AsyncIterator[dict[str, ModelAccount]]:
    """Workspace + two active accounts (opus, sonnet)."""
    opus = _acct(workspace_id, "opus")
    sonnet = _acct(workspace_id, "sonnet")
    async with maker() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add_all([opus, sonnet])
        await s.commit()
    yield {"opus": opus, "sonnet": sonnet}


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch) -> None:
    """Replace the embedder builder so category applies never hit a provider."""
    import backend.api.v1.run_routing as rr

    async def _fake_builder(session, *, workspace_id, account_id):
        return _StubEmbedder()

    monkeypatch.setattr("backend.embedding.authoring.build_account_embedder", _fake_builder)
    # The helper imports build_account_embedder inside the function body, so the
    # source-module patch above is what takes effect.
    assert rr  # keep the import used


async def test_apply_category_creates_intent_and_rule(
    maker, workspace_id, account_id, seeded
) -> None:
    proposal = ApplyProposal(
        name="marketing → sonnet",
        target="sonnet",
        intent_name="marketing",
        intent_examples=["write a marketing email", "plan a campaign", "draft copy"],
        condition={"field": "classified_intent", "operator": "eq", "value": "marketing"},
    )
    async with maker() as s:
        created = await apply_proposals(
            s, workspace_id=workspace_id, account_id=account_id, proposals=[proposal]
        )
    assert len(created) == 1

    async with maker() as s:
        # Intent definition was created (scoped to the personal account_id).
        intents = (
            (
                await s.execute(
                    select(IntentDefinitionRow).where(
                        IntentDefinitionRow.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [i.name for i in intents] == ["marketing"]

        # Rule keyed on classified_intent == marketing.
        rules = (
            (
                await s.execute(
                    select(RunRoutingRuleRow).where(RunRoutingRuleRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rules) == 1
        assert rules[0].target == "sonnet"
        assert rules[0].conditions == [
            {"field": "classified_intent", "operator": "eq", "value": "marketing", "negate": False}
        ]


async def test_apply_complexity_creates_plain_rule(maker, workspace_id, account_id, seeded) -> None:
    proposal = ApplyProposal(
        name="big → opus",
        target="opus",
        condition={"field": "estimated_tokens", "operator": "gt", "value": 2000},
    )
    async with maker() as s:
        await apply_proposals(
            s, workspace_id=workspace_id, account_id=account_id, proposals=[proposal]
        )
    async with maker() as s:
        rules = (
            (
                await s.execute(
                    select(RunRoutingRuleRow).where(RunRoutingRuleRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rules) == 1
        assert rules[0].caller_id is None
        assert rules[0].conditions == [
            {"field": "estimated_tokens", "operator": "gt", "value": 2000, "negate": False}
        ]
        # No intent def created for a non-category proposal.
        intents = (
            (
                await s.execute(
                    select(IntentDefinitionRow).where(
                        IntentDefinitionRow.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert intents == []


async def test_apply_default_sets_workspace_default(
    maker, workspace_id, account_id, seeded
) -> None:
    proposal = ApplyProposal(name="rest → opus", target="opus", is_default=True)
    async with maker() as s:
        created = await apply_proposals(
            s, workspace_id=workspace_id, account_id=account_id, proposals=[proposal]
        )
    # Default sets the workspace pointer — no rule row is created for it.
    assert created == []
    async with maker() as s:
        ws = await s.get(WorkspaceRow, workspace_id)
        assert ws is not None
        assert ws.default_account_id == seeded["opus"].id
        rules = (
            (
                await s.execute(
                    select(RunRoutingRuleRow).where(RunRoutingRuleRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert rules == []


async def test_apply_caller_creates_rule(maker, workspace_id, account_id, seeded) -> None:
    proposal = ApplyProposal(
        name="design → opus", target="opus", caller_id="workflow.agent_loop.plan"
    )
    async with maker() as s:
        await apply_proposals(
            s, workspace_id=workspace_id, account_id=account_id, proposals=[proposal]
        )
    async with maker() as s:
        rules = (
            (
                await s.execute(
                    select(RunRoutingRuleRow).where(RunRoutingRuleRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rules) == 1
        assert rules[0].caller_id == "workflow.agent_loop.plan"
        assert rules[0].conditions == []


async def test_apply_mixed_batch(maker, workspace_id, account_id, seeded) -> None:
    proposals = [
        ApplyProposal(
            name="marketing → sonnet",
            target="sonnet",
            intent_name="marketing",
            intent_examples=["write a marketing email", "plan a campaign", "draft copy"],
        ),
        ApplyProposal(
            name="big → opus",
            target="opus",
            condition={"field": "estimated_tokens", "operator": "gt", "value": 2000},
        ),
        ApplyProposal(name="rest → sonnet", target="sonnet", is_default=True),
    ]
    async with maker() as s:
        created = await apply_proposals(
            s, workspace_id=workspace_id, account_id=account_id, proposals=proposals
        )
    assert len(created) == 2  # category + condition (default sets the pointer)
    async with maker() as s:
        ws = await s.get(WorkspaceRow, workspace_id)
        assert ws is not None
        assert ws.default_account_id == seeded["sonnet"].id
        intents = (
            (
                await s.execute(
                    select(IntentDefinitionRow).where(
                        IntentDefinitionRow.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [i.name for i in intents] == ["marketing"]


async def test_apply_is_atomic_on_unknown_target(maker, workspace_id, account_id, seeded) -> None:
    """A later proposal with a non-active target rolls back the WHOLE batch —
    the earlier category's intent + rule must NOT persist."""
    proposals = [
        ApplyProposal(
            name="marketing → sonnet",
            target="sonnet",
            intent_name="marketing",
            intent_examples=["write a marketing email", "plan a campaign", "draft copy"],
        ),
        ApplyProposal(
            name="bad → ghost",
            target="ghost-model",
            condition={"field": "detected_language", "operator": "eq", "value": "en"},
        ),
    ]
    with pytest.raises(ApplyError):
        async with maker() as s:
            await apply_proposals(
                s, workspace_id=workspace_id, account_id=account_id, proposals=proposals
            )
    async with maker() as s:
        intents = (
            (
                await s.execute(
                    select(IntentDefinitionRow).where(
                        IntentDefinitionRow.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        rules = (
            (
                await s.execute(
                    select(RunRoutingRuleRow).where(RunRoutingRuleRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert intents == []  # rolled back
        assert rules == []


async def test_apply_proposal_rejects_category_without_examples() -> None:
    with pytest.raises(ValueError, match="intent_examples"):
        ApplyProposal(name="x", target="opus", intent_name="marketing", intent_examples=[])


async def test_apply_proposal_rejects_shapeless_non_default() -> None:
    with pytest.raises(ValueError, match="caller_id or a condition"):
        ApplyProposal(name="x", target="opus")


async def test_apply_proposal_rejects_unknown_condition_field() -> None:
    with pytest.raises(ValueError, match="unknown condition field"):
        ApplyProposal(
            name="x",
            target="opus",
            condition={"field": "made_up", "operator": "eq", "value": "x"},
        )


async def test_apply_endpoint_round_trip(maker, workspace_id, account_id, seeded) -> None:
    """POST /api/v1/run-routing/compile/apply persists via the shared helper and
    returns the created rules + default_set flag."""
    import httpx

    from backend.api.deps import (
        get_current_user,
        get_db_session,
        get_workspace_id,
        require_account_id,
    )
    from backend.api.main import create_app

    from .._support import fake_current_user

    app = create_app()

    async def _session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_account_id] = lambda: account_id
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/api/v1/run-routing/compile/apply",
            json={
                "proposals": [
                    {
                        "name": "big → opus",
                        "target": "opus",
                        "condition": {
                            "field": "estimated_tokens",
                            "operator": "gt",
                            "value": 2000,
                        },
                    },
                    {"name": "rest → sonnet", "target": "sonnet", "is_default": True},
                ]
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["default_set"] is True
    assert len(body["created"]) == 1
    assert body["created"][0]["target"] == "opus"

    async with maker() as s:
        ws = await s.get(WorkspaceRow, workspace_id)
        assert ws is not None and ws.default_account_id == seeded["sonnet"].id


async def test_apply_endpoint_unknown_target_422(maker, workspace_id, account_id, seeded) -> None:
    import httpx

    from backend.api.deps import (
        get_current_user,
        get_db_session,
        get_workspace_id,
        require_account_id,
    )
    from backend.api.main import create_app

    from .._support import fake_current_user

    app = create_app()

    async def _session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_account_id] = lambda: account_id
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/api/v1/run-routing/compile/apply",
            json={"proposals": [{"name": "bad", "target": "ghost", "is_default": True}]},
        )
    assert r.status_code == 422, r.text
