"""Executor account routing — provider='executor' still routes through the
native :class:`RunOrchestrator`.

Lift E3 collapsed the old executor-pool wrapper: the factory no longer builds a
full-run ``ExecutorOrchestrator`` for ``provider='executor'`` accounts. Every
account — executor or not — now routes through the native
:class:`~backend.workflow.application.agent_loop.RunOrchestrator`; an executor
account just means each plan/act/judge LLM turn dispatches a one-shot CLI
subprocess via :class:`backend.dispatch.adapter.ExecutorAdapter`. The former
full-run ``ExecutorOrchestrator`` subsystem has been deleted (INV-7); its chat
path is covered by ``tests/dispatch/test_adapter.py``.

This file pins the surviving live invariant: the factory builds a native
``RunOrchestrator`` for both executor and non-executor accounts, and wires a
real (non-None) CanonRetriever into it.

Runs on in-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL``
is set (mirrors the other glue tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.executors.db import WorkerRow
from backend.router.accounts.models import ModelAccount
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.run import build_agent_execution_deps

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _short_timeout_settings(timeout_s: float = 30.0):
    """Settings with a SHORT ``executor_task_timeout_s`` so a per-run
    orchestrator built with this config never blocks on the 1800s prod default
    during a smoke test. Threaded through :func:`build_agent_execution_deps`."""
    return get_settings().model_copy(update={"executor_task_timeout_s": timeout_s})


async def _make_redis() -> Any:
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - fakeredis is a declared dep
        pytest.skip("fakeredis not installed")
    # Isolated server per instance: the default shared server binds its async
    # primitives to the first event loop that touches it, so reuse across
    # pytest-asyncio's per-test loops raises cross-loop Future errors.
    client = fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    await client.flushdb()
    return client


async def _seed_worker(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    capabilities: list[str],
) -> WorkerRow:
    worker = WorkerRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="mac-mini",
        labels=[],
        capabilities=list(capabilities),
        status="online",
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=1),
        token_hash="0" * 64,
        is_active=True,
    )
    s.add(worker)
    await s.flush()
    return worker


async def _seed_executor_account(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    worker_id: uuid.UUID,
    executor_type: str,
) -> ModelAccount:
    """Seed an executor ModelAccount and set it as the workspace default
    so the Lift E2 resolver routes the agent loop's act turn to it without
    an explicit rule."""
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415

    account = ModelAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        account_id=uuid.uuid4(),
        provider="executor",
        label="mac-mini",
        litellm_model=f"executor/{executor_type}",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"worker_id": str(worker_id), "executor_type": executor_type},
    )
    s.add(account)
    await s.flush()
    ws = (
        await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one_or_none()
    if ws is None:
        ws = WorkspaceRow(
            id=workspace_id,
            name="test-ws",
            region="us-1",
            safe_mode=True,
            legal_basis="contract",
        )
        s.add(ws)
        await s.flush()
    ws.default_account_id = account.id
    await s.flush()
    return account


async def _open_run(s: AsyncSession, *, workspace_id: uuid.UUID, text: str) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={"intent_text": text},
    )
    s.add(run)
    await s.flush()
    return run.id


# --------------------------------------------------------------------------
# 1. Non-executor (api-llm) account builds the native RunOrchestrator.
# --------------------------------------------------------------------------


async def test_non_executor_account_builds_native_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from backend.config import get_settings as _get_settings
    from backend.dispatch import adapter as runtime_dispatcher
    from backend.router.llm_client import LlmClient
    from backend.workflow.application.agent_loop import RunOrchestrator
    from backend.workflow.infrastructure.workers import (
        run as run_module,  # noqa: F401 — legacy alias
    )

    # The native path eagerly builds the credential cipher (to decrypt the
    # account's api key) — provide a test KMS key so it constructs. It also
    # builds ``LlmClient()`` which lazily imports litellm (not a declared dep);
    # patch it to a no-op client so the smoke test exercises the native
    # RunOrchestrator branch without a real LLM dep.
    # Lift §17.2a: LlmClient lookup moved to runtime.dispatcher.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    _get_settings.cache_clear()
    monkeypatch.setattr(
        runtime_dispatcher, "LlmClient", lambda: LlmClient(completion_fn=lambda **_: None)
    )

    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415
    from backend.router.accounts.crypto import CredentialCipher  # noqa: PLC0415

    workspace_id = uuid.uuid4()
    async with sf() as s:
        cipher = CredentialCipher(b"0" * 32)
        account = ModelAccount(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            account_id=uuid.uuid4(),
            provider="anthropic",
            label="claude",
            litellm_model="claude-3-5-sonnet",
            api_base=None,
            api_key_encrypted=cipher.encrypt("sk-test"),
            data_jurisdiction="us",
            is_active=True,
            extra_params={},
        )
        s.add(account)
        await s.flush()
        # Lift E2 — resolver needs the workspace default to route the
        # native run without an explicit rule.
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="ws",
                region="us-1",
                safe_mode=True,
                legal_basis="contract",
                default_account_id=account.id,
            )
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="native run")
        await s.commit()

    deps = build_agent_execution_deps()
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, RunOrchestrator)


# --------------------------------------------------------------------------
# 2. Lift E3 — executor accounts also route through the native RunOrchestrator.
#    The executor's CLI subprocess is reached one turn at a time through
#    ExecutorAdapter.chat; the legacy full-run wrapper no longer exists.
# --------------------------------------------------------------------------


async def test_lift_e3_executor_account_routes_through_native_run_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Lift E3 invariant: executor account → native RunOrchestrator.

    The factory used to branch on ``is_executor_account`` and build a
    full-run wrapper that drove the whole run via a single CLI subprocess.
    After Lift E3 every account routes through the native
    :class:`RunOrchestrator`; an executor account just means each
    plan/act/judge LLM turn dispatches a one-shot CLI subprocess via
    :class:`backend.dispatch.adapter.ExecutorAdapter`.
    """
    from backend.workflow.application.agent_loop import RunOrchestrator

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["claude_code"])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship it")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis, settings=_short_timeout_settings())
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, RunOrchestrator)
    await redis.aclose()


# --------------------------------------------------------------------------
# 3. Timeout setting default sanity.
# --------------------------------------------------------------------------


async def test_executor_task_timeout_setting_default() -> None:
    assert get_settings().executor_task_timeout_s == 1800.0


# --------------------------------------------------------------------------
# 4. B3 — _factory injects a REAL (non-None) CanonRetriever into the native
#    RunOrchestrator. Prior state: retriever was ALWAYS None in prod (RC-2), so
#    BSage canon was never folded into verification. The delta asserted here is
#    None → a workspace-scoped retriever on the orchestrator.
# --------------------------------------------------------------------------


def _vault_root_settings(tmp_path: Path, timeout_s: float = 30.0):
    """Settings pointing the knowledge vault root at ``tmp_path`` (so the
    factory's retriever reads the test-seeded canon) + a short executor timeout."""
    return get_settings().model_copy(
        update={
            "knowledge_vault_root": str(tmp_path / "vault"),
            "executor_task_timeout_s": timeout_s,
        }
    )


async def _seed_canon_concept(
    *,
    vault_root: Path,
    region: str,
    workspace_id: uuid.UUID,
    concept_id: str,
    display: str,
) -> None:
    from backend.knowledge.canonicalization import models  # noqa: PLC0415
    from backend.knowledge.canonicalization.store import NoteStore  # noqa: PLC0415
    from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415

    store = NoteStore(FileSystemStorage(vault_root / region / str(workspace_id)))
    await store.write_concept(
        models.ConceptEntry(
            concept_id=concept_id,
            path=f"concepts/active/{concept_id}.md",
            display=display,
            aliases=[],
            created_at=datetime(2026, 5, 6, tzinfo=UTC),
            updated_at=datetime(2026, 5, 6, tzinfo=UTC),
        )
    )


async def test_factory_wires_retriever_into_native_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3 delta: the production factory now passes a non-None retriever to the
    native RunOrchestrator (was None)."""
    import base64

    from backend.config import get_settings as _get_settings
    from backend.dispatch import adapter as runtime_dispatcher
    from backend.router.llm_client import LlmClient
    from backend.workflow.application.agent_loop import RunOrchestrator
    from backend.workflow.infrastructure.workers import (
        run as run_module,  # noqa: F401 — legacy alias
    )

    # Lift §17.2a: LlmClient lookup moved to runtime.dispatcher.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    _get_settings.cache_clear()
    monkeypatch.setattr(
        runtime_dispatcher, "LlmClient", lambda: LlmClient(completion_fn=lambda **_: None)
    )

    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415
    from backend.router.accounts.crypto import CredentialCipher  # noqa: PLC0415

    settings = _vault_root_settings(tmp_path)
    workspace_id = uuid.uuid4()
    await _seed_canon_concept(
        vault_root=Path(settings.knowledge_vault_root),
        region=settings.knowledge_default_region,
        workspace_id=workspace_id,
        concept_id="structured-logging",
        display="Use structlog for structured logging",
    )
    async with sf() as s:
        cipher = CredentialCipher(b"0" * 32)
        account = ModelAccount(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            account_id=uuid.uuid4(),
            provider="anthropic",
            label="claude",
            litellm_model="claude-3-5-sonnet",
            api_base=None,
            api_key_encrypted=cipher.encrypt("sk-test"),
            data_jurisdiction="us",
            is_active=True,
            extra_params={},
        )
        s.add(account)
        await s.flush()
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="ws",
                region="us-1",
                safe_mode=True,
                legal_basis="contract",
                default_account_id=account.id,
            )
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="native run")
        await s.commit()

    deps = build_agent_execution_deps(settings=settings)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, RunOrchestrator)
        # The delta: a real retriever is wired (NOT None as before B3).
        assert orchestrator._retriever is not None  # noqa: SLF001 — wiring invariant
        patterns = await orchestrator._retriever.retrieve_for_signals(  # noqa: SLF001
            "added structured logging throughout\napp.py"
        )
        assert "Use structlog for structured logging" in patterns
