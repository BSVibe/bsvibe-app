"""Worker runtime — production deps graph drives a Direct run end to end.

This proves the worker-runtime chunk's contract: a seeded workspace WITH one
active ModelAccount drives a Direct run to REVIEW_READY + a delivered artifact
through the **real** dependency-construction path
(:func:`backend.workers.run.build_agent_execution_deps` →
:func:`resolve_workspace_model_account` → :func:`build_gateway_dispatcher` →
:class:`GatewayLoopLlm`), and the model-account resolution policy creates a
:class:`Decision` (run stays RUNNING) when there is no active account.

CI-safe: no Docker (``NoopSandboxManager`` is injected) and no real model — the
gateway work-LLM is stubbed at the :class:`LlmClient` ``completion_fn`` boundary
(the documented LLM seam), so every layer above it is the real production code.
Runs on in-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL``
is set (mirrors the other glue tests).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.config import get_settings
from backend.delivery.db import DeliveryEventRow
from backend.execution.db import Decision, Deliverable, ExecutionRun, RunStatus
from backend.intake.db import RequestRow, RequestStatus
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.schemas import ModelAccountCreate
from backend.router.accounts.service import ModelAccountService
from backend.router.llm_client import LlmClient
from backend.supervisor.sandbox import NoopSandboxManager
from backend.workers import run as runtime
from backend.workers.agent_worker import AgentWorker
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workers.intake_worker import IntakeWorker

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

# A deterministic 32-byte AES key (base64-url) so CredentialCipher can encrypt
# the seeded account's api_key without a real KMS key.
_TEST_KMS_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded_product(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> uuid.UUID:
    """L-P1: every direct-path message now requires a product binding.
    Seeds Workspace + Product (PG enforces products.workspace_id FK)."""
    from backend.workspaces.db import ProductRow, WorkspaceRow

    product_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="test-workspace",
                safe_mode=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="test-product",
                slug="test-product",
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return product_id


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def kms_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a real (test) KMS key + a tmp run root, and clear the settings cache
    so the production deps path reads them."""
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", _TEST_KMS_KEY_B64)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# Stubbed gateway work-LLM (injected at the LlmClient boundary)
# --------------------------------------------------------------------------


class _ScriptedCompletion:
    """A scripted ``litellm.acompletion`` — pops the next response FIFO.

    Returned shape mirrors litellm's: an object with ``.choices[0].message``
    (``.content`` + ``.tool_calls``) and ``.usage``. The real
    :class:`LlmClient` normalizes it; everything above is production code."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)

    async def __call__(self, **_kwargs: Any) -> SimpleNamespace:
        if not self._turns:
            raise AssertionError("scripted completion exhausted")
        turn = self._turns.pop(0)
        tool_calls = [
            SimpleNamespace(
                id=tc["id"],
                type="function",
                function=SimpleNamespace(name=tc["name"], arguments=json.dumps(tc["arguments"])),
            )
            for tc in turn.get("tool_calls", [])
        ]
        message = SimpleNamespace(content=turn.get("content", ""), tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


def _verified_script() -> _ScriptedCompletion:
    """declare a command check + write the artifact, then plain text → verified.

    B9a — the production deps now make a real cheap-LLM FRAME call before the work
    loop drives (FrameStage uses the gateway-resolved cheap LLM). So the script
    leads with the frame turn: a JSON framing the FrameStage parses (skill match
    by description against the workspace catalog), then the two work turns."""
    return _ScriptedCompletion(
        [
            {
                "content": json.dumps(
                    {
                        "framed_intent": "Build the answer file",
                        "skill_match": "weekly-digest",
                        "artifact_type_hint": "code",
                        "path_classification": "agent_loop",
                    }
                ),
                "tool_calls": [],
            },
            {
                "content": "Writing the deliverable and declaring how to check it.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "declare_verification",
                        "arguments": {
                            "checks": [{"kind": "command", "command": "test -f answer.txt"}]
                        },
                    },
                    {
                        "id": "c2",
                        "name": "file_write",
                        "arguments": {"path": "answer.txt", "content": "42\n"},
                    },
                ],
            },
            {"content": "Done — answer.txt written.", "tool_calls": []},
        ]
    )


def _patch_scripted_llm(monkeypatch: pytest.MonkeyPatch, script: _ScriptedCompletion) -> None:
    """Make ``build_gateway_dispatcher``'s ``LlmClient()`` use the scripted
    completion, leaving the rest of the real deps graph untouched."""
    scripted_client = LlmClient(completion_fn=script)
    monkeypatch.setattr(runtime, "LlmClient", lambda: scripted_client)


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession],
    founder_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_active_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    label: str = "default",
) -> uuid.UUID:
    async with sf() as s:
        svc = ModelAccountService(s, cipher=runtime.CredentialCipher(runtime._key_from_settings()))
        out = await svc.create(
            workspace_id=workspace_id,
            account_id=account_id,
            payload=ModelAccountCreate(
                provider="ollama",
                label=label,
                litellm_model="ollama_chat/qwen3-coder:30b",
                api_key="sk-test",
                data_jurisdiction="us",
            ),
        )
        await s.commit()
        return out.id


# --------------------------------------------------------------------------
# Smoke / integration — production deps drive a Direct run to delivery
# --------------------------------------------------------------------------


async def test_production_deps_drive_direct_run_to_delivery(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    seeded_product: uuid.UUID,
    kms_key: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Seed one ACTIVE model account for the workspace.
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=account_id)
    _patch_scripted_llm(monkeypatch, _verified_script())

    # 1. Founder POSTs a direct message; intake drains it into a Request.
    resp = await client.post("/api/v1/messages", json={"text": "build the answer file"})
    assert resp.status_code == 202, resp.text
    assert await IntakeWorker(session_factory=sf).drain_once() == 1

    # 2. AgentWorker uses the REAL production AgentExecutionDeps (Noop sandbox
    #    injected; gateway work-LLM stubbed at the LlmClient boundary).
    settings = get_settings()
    deps = runtime.build_agent_execution_deps(
        settings=settings, sandbox_manager=NoopSandboxManager()
    )
    # Drive runs inside tmp_path so the test never writes the repo's var/runs.
    deps.workspace_root = tmp_path
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.claim_once() == 1
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        # W2: verified product runs auto-merge to main + transition to
        # SHIPPED immediately (the Direct-path used to stop at
        # REVIEW_READY before W2's auto-ship landed).
        assert run.status is RunStatus.SHIPPED
        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        assert "answer.txt" in (deliverable.payload.get("artifact_refs") or [])
        event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert event.deliverable_id == deliverable.id
        deliverable_id = deliverable.id
        run_id = run.id

    # The work LLM actually wrote the artifact into the run's workspace.
    assert (tmp_path / str(run_id) / "answer.txt").read_text() == "42\n"

    # 3. DeliveryWorker drains the event through the REAL plugin dispatcher.
    adapter = await runtime.build_delivery_adapter(session_factory=sf)
    delivery = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await delivery.drain_once() == 1
    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None
    # No matching plugin is fine — the event still drains (queue never wedges).
    assert deliverable_id is not None


# --------------------------------------------------------------------------
# Resolution policy — zero active accounts → Decision, run stays RUNNING
# --------------------------------------------------------------------------


async def test_zero_active_accounts_creates_decision_and_run_stays_running(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    seeded_product: uuid.UUID,
    kms_key: None,
    tmp_path: Path,
) -> None:
    # NO model account is seeded for the workspace.
    resp = await client.post("/api/v1/messages", json={"text": "do the work"})
    assert resp.status_code == 202
    assert await IntakeWorker(session_factory=sf).drain_once() == 1

    deps = runtime.build_agent_execution_deps(
        settings=get_settings(), sandbox_manager=NoopSandboxManager()
    )
    deps.workspace_root = tmp_path
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.claim_once() == 1
    # drive_once returns 1 (the run was visited) but the run is NOT driven —
    # the factory created a Decision and returned None.
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        # Run stays RUNNING (paused on the Decision) — never silently stalled,
        # never crashed, never advanced to REVIEW_READY/FAILED.
        assert run.status is RunStatus.RUNNING
        req = (await s.execute(select(RequestRow))).scalar_one()
        assert req.status is RequestStatus.RUNNING
        decision = (await s.execute(select(Decision))).scalar_one()
        assert decision.decision == runtime.DECISION_NO_MODEL_ACCOUNT
        assert "no active model account" in (decision.rationale or "")
        # No deliverable / delivery event was produced.
        assert (await s.execute(select(Deliverable))).first() is None
        assert (await s.execute(select(DeliveryEventRow))).first() is None


async def test_two_active_same_class_accounts_resolve_via_d4_no_decision(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    seeded_product: uuid.UUID,
    kms_key: None,
    tmp_path: Path,
) -> None:
    """D4 delta — TWO active same-class accounts no longer STALL.

    Pre-D4 this raised an ``ambiguous_model_account`` :class:`Decision` (a stall
    on 2+). D4 makes :func:`resolve_route` pick deterministically within the
    class (highest ``routing_priority``, tiebroken by ``created_at`` then ``id``)
    — a SPECIFIC account, never an ambiguous Decision."""
    from backend.routing.engine import resolve_route  # noqa: PLC0415
    from backend.routing.multi_account import ROUTING_PRIORITY_KEY  # noqa: PLC0415

    # Two active local (ollama) accounts; "b" carries the higher priority.
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=account_id, label="a")
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=uuid.uuid4(), label="b")
    resp = await client.post("/api/v1/messages", json={"text": "two accounts run"})
    assert resp.status_code == 202
    assert await IntakeWorker(session_factory=sf).drain_once() == 1

    # Claim the Request into an ExecutionRun (the resolution input) without
    # driving the loop — D4's contract is at resolution, not execution.
    deps = runtime.build_agent_execution_deps(
        settings=get_settings(), sandbox_manager=NoopSandboxManager()
    )
    deps.workspace_root = tmp_path
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.claim_once() == 1

    async with sf() as s:
        # Give "b" an explicit higher routing priority so the winner is pinned.
        accounts = list((await s.execute(select(ModelAccount))).scalars().all())
        for acct in accounts:
            if acct.label == "b":
                acct.extra_params = {ROUTING_PRIORITY_KEY: 5}
        await s.commit()

        run = (await s.execute(select(ExecutionRun))).scalar_one()
        resolved = await resolve_route(s, run)
        await s.commit()

        # A specific account resolved — no stall, no ambiguous Decision.
        assert resolved is not None
        assert resolved.label == "b"
        assert (await s.execute(select(Decision))).first() is None


# --------------------------------------------------------------------------
# Per-workspace skill scoping — two workspaces resolve to two skill roots
# --------------------------------------------------------------------------


def _write_skill(root: Path, name: str, description: str) -> None:
    """Write a minimal Workflow §6 #5 skill manifest under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\nversion: 1\ndescription: {description}\n---\nbody",
        encoding="utf-8",
    )


async def test_skill_loader_for_resolves_per_workspace_roots(
    tmp_path: Path,
) -> None:
    """The production factory roots each workspace at ``<skills_root>/<ws>/``.

    Seed a skill in workspace A's dir, none in B's — A's loader sees the skill,
    B's loader sees an empty registry. Proves skill loading is per-workspace,
    not a single shared root-level set. (``async`` only to satisfy the module's
    ``pytestmark = pytest.mark.asyncio``; the body is synchronous.)
    """
    skills_root = tmp_path / "skills"
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    _write_skill(skills_root / str(ws_a), "weekly-digest", "Generate a weekly digest")

    settings = get_settings().model_copy(update={"skills_root": str(skills_root)})
    deps = runtime.build_agent_execution_deps(
        settings=settings, sandbox_manager=NoopSandboxManager()
    )

    loader_a = deps.skill_loader_for(ws_a)
    loader_b = deps.skill_loader_for(ws_b)

    # Distinct roots, scoped by workspace_id.
    assert loader_a._skill_dir == skills_root / str(ws_a)
    assert loader_b._skill_dir == skills_root / str(ws_b)
    assert loader_a._skill_dir != loader_b._skill_dir
    # A sees its seeded skill; B (no dir seeded) sees nothing.
    assert set(loader_a.registry) == {"weekly-digest"}
    assert loader_b.registry == {}


async def test_drive_frames_against_only_the_runs_workspace_skills(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    seeded_product: uuid.UUID,
    kms_key: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: a run for workspace A frames against A's skill only.

    Seed a skill in workspace A's skills dir and NONE in another workspace's
    dir. Drive an A run; its ``frame.skill_match`` resolves to A's skill —
    proving the worker uses a SkillLoader rooted at the run's own workspace.
    """
    skills_root = tmp_path / "skills"
    other_ws = uuid.uuid4()
    # Skill in A's dir; the request text references it so FrameStage matches.
    _write_skill(skills_root / str(workspace_id), "weekly-digest", "Generate a weekly digest")
    # An UNRELATED workspace's dir exists with a different skill — must NOT leak.
    _write_skill(skills_root / str(other_ws), "groceries", "Buy groceries from the store")

    await _seed_active_account(sf, workspace_id=workspace_id, account_id=account_id)
    _patch_scripted_llm(monkeypatch, _verified_script())

    resp = await client.post("/api/v1/messages", json={"text": "please run the weekly digest now"})
    assert resp.status_code == 202, resp.text
    assert await IntakeWorker(session_factory=sf).drain_once() == 1

    settings = get_settings().model_copy(update={"skills_root": str(skills_root)})
    deps = runtime.build_agent_execution_deps(
        settings=settings, sandbox_manager=NoopSandboxManager()
    )
    deps.workspace_root = tmp_path / "runs"
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.claim_once() == 1
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        # Framed against workspace A's skills only — sees A's "weekly-digest",
        # never the other workspace's "groceries".
        assert run.payload["frame"]["skill_match"] == "weekly-digest"


# --------------------------------------------------------------------------
# Runtime construction — the worker set is built + shuts down gracefully
# --------------------------------------------------------------------------


async def test_build_worker_runtime_constructs_all_workers(
    sf: async_sessionmaker[AsyncSession],
    kms_key: None,
) -> None:
    deps = runtime.build_agent_execution_deps(
        settings=get_settings(), sandbox_manager=NoopSandboxManager()
    )
    adapter = await runtime.build_delivery_adapter(session_factory=sf)
    rt = runtime.build_worker_runtime(session_factory=sf, execution=deps, delivery_adapter=adapter)
    names = {w._name for w in rt.workers}
    assert names == {
        "intake_worker",
        "agent_worker",
        "delivery_worker",
        "settle_worker",
        "relay_worker",
        # M1 — schedule runner now ships in the production worker set.
        "schedule_worker",
        # D3a — Safe Mode expiry sweep. A SECOND ScheduleWorker against the
        # same ScheduleRunnerProtocol seam, sweeping expired Safe Mode
        # queue rows system-wide and emitting a ``safe_mode.expired`` audit
        # row tagged ``trigger=schedule, source=system.safe_mode_expiry``.
        "safe_mode_expiry_worker",
    }
    # start + graceful stop is idempotent and drains in-flight ticks.
    for w in rt.workers:
        await w.start()
    await rt.shutdown()
    assert all(w._task is None for w in rt.workers)


# --------------------------------------------------------------------------
# Settle entity-extractor factory — concepts from LLM-extracted entities
# --------------------------------------------------------------------------


async def test_settle_entity_extractor_factory_extracts_entities(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    kms_key: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With ONE active account, the factory builds an IngestCompiler whose
    CompileLlm seam routes through the gateway; the scripted LLM returns an entity
    plan and ``extract_entity_names`` surfaces the committed entities. The LLM is
    stubbed at the LlmClient boundary (never a real model)."""
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    get_settings.cache_clear()
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=account_id)

    # The dispatcher returns the IngestCompiler's plan as the response content.
    plan = json.dumps(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "Calculator",
                "content": "Built a [[calculator]] in [[Python]].",
                "tags": ["math"],
                "entities": ["[[calculator]]", "[[Python]]"],
                "reason": "r",
                "source_seeds": [1],
                "related": [],
            }
        ]
    )
    _patch_scripted_llm(monkeypatch, _ScriptedCompletion([{"content": plan}]))

    factory = runtime.build_settle_entity_extractor_factory(
        session_factory=sf, settings=get_settings()
    )
    extractor = await factory(region="us-1", workspace_id=workspace_id)
    assert extractor is not None
    names = await extractor.extract_entity_names("build a calculator in python")
    assert names == ["calculator", "Python"]
    get_settings.cache_clear()


def _bare_account(provider: str) -> Any:
    from backend.router.accounts.models import ModelAccount

    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        provider=provider,
        label=provider,
        litellm_model=f"{provider}/model",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="us",
        is_active=True,
        extra_params={},
    )


async def test_single_native_account_ignores_executor_accounts() -> None:
    # The cheap-LLM resolvers (frame stage + settle extractor) must pick the
    # lone active NON-executor account even when executor worker accounts
    # coexist — registering an executor worker must not break native framing.
    native = _bare_account("ollama")
    execs = [_bare_account("executor"), _bare_account("executor")]

    # 1 native + N executor → the native one (the regression case).
    assert runtime._single_native_account([native, *execs]) is native
    # exactly 1 native, any order → native.
    assert runtime._single_native_account([execs[0], native]) is native
    # 2 native → ambiguous → None (never guess among native models).
    assert runtime._single_native_account([native, _bare_account("ollama")]) is None
    # only executor accounts → None (can't drive cheap LLM).
    assert runtime._single_native_account(execs) is None
    # empty → None.
    assert runtime._single_native_account([]) is None


async def test_settle_entity_extractor_factory_none_when_no_account(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    kms_key: None,
) -> None:
    """Zero active accounts → None (soft-fallback signal), never a guess."""
    factory = runtime.build_settle_entity_extractor_factory(
        session_factory=sf, settings=get_settings()
    )
    assert await factory(region="us-1", workspace_id=workspace_id) is None


async def test_settle_entity_extractor_factory_none_when_ambiguous(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    kms_key: None,
) -> None:
    """More than one active account → None (no silent guess for derived knowledge)."""
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=uuid.uuid4(), label="a")
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=uuid.uuid4(), label="b")
    factory = runtime.build_settle_entity_extractor_factory(
        session_factory=sf, settings=get_settings()
    )
    assert await factory(region="us-1", workspace_id=workspace_id) is None
