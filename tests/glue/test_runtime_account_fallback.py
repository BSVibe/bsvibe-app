"""#1073 — the agent runtime keeps the account the fallback found.

``resolve_via_caller`` can miss (no rule target resolves, no workspace default)
while ``resolve_workspace_model_account`` still finds the workspace's one active
account. The factory used to call the fallback, discard its answer and return
``None`` — the run stalled with no Decision and no error (prod cc68f583).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.schemas import ModelAccountCreate
from backend.router.accounts.service import ModelAccountService
from backend.workflow.application.agent_loop import RunOrchestrator
from backend.workflow.domain.model_account_decision import DECISION_NO_MODEL_ACCOUNT
from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunStatus
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers import run as runtime

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_TEST_KMS_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def kms_key(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", _TEST_KMS_KEY_B64)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_workspace(
    sf: async_sessionmaker[AsyncSession], *, workspace_id: uuid.UUID, with_account: bool
) -> None:
    """A workspace with NO default_account_id and NO rules — only the legacy
    exactly-one-active heuristic can find the account."""
    async with sf() as s, workspace_session_scope(s, workspace_id):
        s.add(WorkspaceRow(id=workspace_id, name="ws", safe_mode=True, legal_basis="contract"))
        await s.flush()
        if with_account:
            svc = ModelAccountService(
                s, cipher=runtime.CredentialCipher(runtime._key_from_settings())
            )
            await svc.create(
                workspace_id=workspace_id,
                account_id=uuid.uuid4(),
                payload=ModelAccountCreate(
                    provider="ollama",
                    label="only",
                    litellm_model="ollama_chat/qwen3-coder:30b",
                    api_key="sk-test",
                    extra_params={},
                ),
            )
        await s.commit()


async def _factory_result(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> tuple[object, list[Decision]]:
    async with sf() as session, workspace_session_scope(session, workspace_id):
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            request_id=None,
            status=RunStatus.RUNNING,
            payload={"intent_text": "do the thing", "frame": {}},
        )
        session.add(run)
        await session.flush()
        deps = runtime.build_agent_execution_deps(
            settings=get_settings(), sandbox_manager=NoopSandboxManager()
        )
        orch = await deps.orchestrator_factory(session, run)
        decisions = list((await session.execute(select(Decision))).scalars())
        return orch, decisions


async def test_fallback_account_drives_the_run(
    sf: async_sessionmaker[AsyncSession], kms_key: None
) -> None:
    workspace_id = uuid.uuid4()
    await _seed_workspace(sf, workspace_id=workspace_id, with_account=True)

    orch, decisions = await _factory_result(sf, workspace_id)

    assert isinstance(orch, RunOrchestrator)
    assert decisions == []


async def test_no_account_at_all_still_leaves_a_decision(
    sf: async_sessionmaker[AsyncSession], kms_key: None
) -> None:
    workspace_id = uuid.uuid4()
    await _seed_workspace(sf, workspace_id=workspace_id, with_account=False)

    orch, decisions = await _factory_result(sf, workspace_id)

    assert orch is None
    assert [d.payload.get("reason") for d in decisions] == [DECISION_NO_MODEL_ACCOUNT]
