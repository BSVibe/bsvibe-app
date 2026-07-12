"""/api/v1/run-routing — create/update a rule FROM an NL condition (Lift N5).

The founder authors ONE rule = a free-text ``source_text`` CONDITION + a
``target`` model. On save the phrase compiles — per single rule — into the
structured ``caller_id`` / ``conditions``; a category also creates an intent def.
Editing ``source_text`` recompiles. An uninterpretable phrase 422s and persists
nothing. The structured create path stays back-compatible. The LLM + embedder are
stubbed — no real provider is hit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.embedding.db  # noqa: F401 — register intent tables
import backend.identity.workspaces_db  # noqa: F401 — register workspaces table
import backend.router.accounts.account_models  # noqa: F401 — register accounts table
import backend.router.accounts.models  # noqa: F401 — register model_accounts table
import backend.router.routing.run_routing.db  # noqa: F401 — register run_routing_rules table
from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
    require_account_id,
)
from backend.api.main import create_app
from backend.embedding.db import IntentDefinitionRow
from backend.embedding.service import EmbeddedExample
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.nl_compile import CompiledCondition

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


class _StubEmbedder:
    model = "stub-embed"

    async def embed_one(self, text: str) -> EmbeddedExample:
        return EmbeddedExample(text=text, embedding=[0.1, 0.2, 0.3], model=self.model)


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
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded(maker, workspace_id) -> AsyncIterator[None]:
    async with maker() as s:
        s.add_all([_acct(workspace_id, "opus"), _acct(workspace_id, "sonnet")])
        await s.commit()
    yield None


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch) -> None:
    async def _fake_builder(session, *, workspace_id, account_id):
        return _StubEmbedder()

    monkeypatch.setattr("backend.embedding.authoring.build_account_embedder", _fake_builder)


@pytest_asyncio.fixture
async def client(maker, workspace_id, account_id) -> AsyncIterator[httpx.AsyncClient]:
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
        yield c


def _stub_compile(monkeypatch, result) -> None:
    """Replace the workspace source-text compiler with a canned result.

    ``result`` is a :class:`CompiledCondition` (success) — the model-resolution +
    LLM call are stubbed out. Pass ``UNINTERPRETABLE`` to simulate a phrase that
    compiles to nothing (the helper raises ``SourceTextUninterpretableError``)."""
    from backend.api.v1.run_routing import SourceTextUninterpretableError

    async def _fake(session, workspace_id, text, *, llm=None):
        if result is UNINTERPRETABLE:
            raise SourceTextUninterpretableError(text)
        return result

    monkeypatch.setattr("backend.api.v1.run_routing.compile_source_text_for_workspace", _fake)


UNINTERPRETABLE = object()


# ---------------------------------------------------------------------------
# Category source_text → intent def + classified_intent rule + stored text
# ---------------------------------------------------------------------------
async def test_create_from_category_source_text(
    client, maker, workspace_id, seeded, monkeypatch
) -> None:
    _stub_compile(
        monkeypatch,
        CompiledCondition(
            condition={"field": "classified_intent", "operator": "eq", "value": "marketing"},
            intent_name="marketing",
            intent_examples=["write a marketing email", "plan a campaign", "draft copy"],
        ),
    )
    r = await client.post(
        "/api/v1/run-routing",
        json={"name": "marketing → sonnet", "source_text": "마케팅 관련", "target": "sonnet"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_text"] == "마케팅 관련"
    assert body["target"] == "sonnet"
    assert body["conditions"] == [
        {"field": "classified_intent", "operator": "eq", "value": "marketing", "negate": False}
    ]

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
        assert [i.name for i in intents] == ["marketing"]


# ---------------------------------------------------------------------------
# Complexity source_text → estimated_tokens rule (no intent)
# ---------------------------------------------------------------------------
async def test_create_from_complexity_source_text(
    client, maker, workspace_id, seeded, monkeypatch
) -> None:
    _stub_compile(
        monkeypatch,
        CompiledCondition(condition={"field": "estimated_tokens", "operator": "gt", "value": 2000}),
    )
    r = await client.post(
        "/api/v1/run-routing",
        json={"name": "big → opus", "source_text": "복잡한 작업", "target": "opus"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_text"] == "복잡한 작업"
    assert body["caller_id"] is None
    assert body["conditions"] == [
        {"field": "estimated_tokens", "operator": "gt", "value": 2000, "negate": False}
    ]

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
        assert intents == []  # no intent def for a non-category dimension


# ---------------------------------------------------------------------------
# Stage source_text → caller_id rule
# ---------------------------------------------------------------------------
async def test_create_from_stage_source_text(client, seeded, monkeypatch) -> None:
    _stub_compile(monkeypatch, CompiledCondition(caller_id="workflow.agent_loop.plan"))
    r = await client.post(
        "/api/v1/run-routing",
        json={"name": "design → opus", "source_text": "설계 단계", "target": "opus"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["caller_id"] == "workflow.agent_loop.plan"
    assert body["conditions"] == []
    assert body["source_text"] == "설계 단계"


# ---------------------------------------------------------------------------
# Uninterpretable source_text → 422, nothing persisted
# ---------------------------------------------------------------------------
async def test_uninterpretable_source_text_422_nothing_persisted(
    client, maker, workspace_id, seeded, monkeypatch
) -> None:
    _stub_compile(monkeypatch, UNINTERPRETABLE)
    r = await client.post(
        "/api/v1/run-routing",
        json={"name": "huh", "source_text": "asdf qwer", "target": "opus"},
    )
    assert r.status_code == 422, r.text
    assert "could not" in r.json()["detail"].lower() or "interpret" in r.json()["detail"].lower()

    # Nothing persisted — no rule, no intent.
    assert (await client.get("/api/v1/run-routing")).json() == []
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
        assert intents == []


# ---------------------------------------------------------------------------
# Back-compat — structured create still works, source_text NULL
# ---------------------------------------------------------------------------
async def test_structured_create_still_works(client, seeded) -> None:
    r = await client.post(
        "/api/v1/run-routing",
        json={
            "name": "impl-stage",
            "caller_id": "workflow.agent_loop.act",
            "target": "opus",
            "conditions": [{"field": "stage", "operator": "eq", "value": "impl"}],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_text"] is None
    assert body["caller_id"] == "workflow.agent_loop.act"


async def test_create_rejects_both_source_text_and_caller_id(client, seeded) -> None:
    r = await client.post(
        "/api/v1/run-routing",
        json={
            "name": "conflict",
            "source_text": "복잡한 작업",
            "caller_id": "workflow.agent_loop.act",
            "target": "opus",
        },
    )
    assert r.status_code == 422


async def test_create_rejects_both_source_text_and_conditions(client, seeded) -> None:
    r = await client.post(
        "/api/v1/run-routing",
        json={
            "name": "conflict2",
            "source_text": "복잡한 작업",
            "target": "opus",
            "conditions": [{"field": "stage", "operator": "eq", "value": "impl"}],
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# PATCH source_text recompiles + rewrites caller_id/conditions
# ---------------------------------------------------------------------------
async def test_patch_source_text_recompiles(
    client, maker, workspace_id, seeded, monkeypatch
) -> None:
    # Start with a complexity rule.
    _stub_compile(
        monkeypatch,
        CompiledCondition(condition={"field": "estimated_tokens", "operator": "gt", "value": 2000}),
    )
    created = (
        await client.post(
            "/api/v1/run-routing",
            json={"name": "r", "source_text": "복잡한 작업", "target": "opus"},
        )
    ).json()
    rule_id = created["id"]

    # Edit source_text → now a language rule + change target.
    _stub_compile(
        monkeypatch,
        CompiledCondition(
            condition={"field": "detected_language", "operator": "eq", "value": "ko"}
        ),
    )
    r = await client.patch(
        f"/api/v1/run-routing/{rule_id}",
        json={"source_text": "한국어 요청", "target": "sonnet"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_text"] == "한국어 요청"
    assert body["target"] == "sonnet"
    assert body["conditions"] == [
        {"field": "detected_language", "operator": "eq", "value": "ko", "negate": False}
    ]
    # The old estimated_tokens condition is gone (rewritten, not appended).
    assert all(c["field"] != "estimated_tokens" for c in body["conditions"])


async def test_patch_uninterpretable_source_text_422(
    client, maker, workspace_id, seeded, monkeypatch
) -> None:
    _stub_compile(
        monkeypatch,
        CompiledCondition(condition={"field": "estimated_tokens", "operator": "gt", "value": 2000}),
    )
    created = (
        await client.post(
            "/api/v1/run-routing",
            json={"name": "r2", "source_text": "복잡한 작업", "target": "opus"},
        )
    ).json()
    rule_id = created["id"]

    _stub_compile(monkeypatch, UNINTERPRETABLE)
    r = await client.patch(f"/api/v1/run-routing/{rule_id}", json={"source_text": "gibberish"})
    assert r.status_code == 422, r.text

    # The original rule is unchanged.
    listed = (await client.get("/api/v1/run-routing")).json()
    assert listed[0]["source_text"] == "복잡한 작업"
    assert listed[0]["conditions"][0]["field"] == "estimated_tokens"


async def test_patch_caller_still_works_and_keeps_source_text_null(client, seeded) -> None:
    """Back-compat: the structured PATCH (caller/target/is_active) is unchanged."""
    created = (
        await client.post(
            "/api/v1/run-routing",
            json={"name": "s", "caller_id": "workflow.agent_loop.plan", "target": "opus"},
        )
    ).json()
    r = await client.patch(
        f"/api/v1/run-routing/{created['id']}",
        json={"caller_id": "workflow.judge", "target": "sonnet"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["caller_id"] == "workflow.judge"
    assert body["source_text"] is None
