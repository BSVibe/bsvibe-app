"""B14 — Dead-code cleanup + liveness hardening.

Three deltas are asserted here:

1. **Dead code is gone.** ``ExecutorDispatchWorker`` and
   :func:`backend.executors.dispatch.claim_pending_task` are no longer importable
   from their module paths. The orphaned alt-dispatch design is retired.

2. **Redis-None startup guard.** When a workspace has any active
   ``provider="executor"`` :class:`ModelAccount` AND ``settings.redis_url`` is
   empty, the startup helper :func:`check_executor_dispatch_health` emits a
   structured WARNING (``executor_dispatch_no_redis``) — never silent, never
   crashing. With Redis configured OR no executor accounts, no warning fires.

3. (Templates + README touched in this lift are static files; covered by a
   simple path-exists assertion.)
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.router.accounts.models import ModelAccount
from backend.workflow.infrastructure.workers.run import check_executor_dispatch_health

from .._support import db_engine

# pytestmark = pytest.mark.asyncio  # individual async tests are marked below;
# top-level mark causes false warnings on sync tests in this module.


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


# ── (1) Dead code removed ─────────────────────────────────────────────────────


def test_executor_dispatch_worker_module_is_gone() -> None:
    """The orphan ``backend.workers.executor_dispatch`` module no longer exists."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.workers.executor_dispatch")


def test_executor_dispatch_worker_not_exported_from_workers() -> None:
    """``ExecutorDispatchWorker`` is not in ``backend.workers``'s public surface."""
    workers = importlib.import_module("backend.workers")
    assert not hasattr(workers, "ExecutorDispatchWorker")
    assert "ExecutorDispatchWorker" not in getattr(workers, "__all__", [])


def test_claim_pending_task_removed_from_dispatch() -> None:
    """``claim_pending_task`` no longer exists in ``backend.executors.dispatch``."""
    dispatch = importlib.import_module("backend.executors.dispatch")
    assert not hasattr(dispatch, "claim_pending_task")
    assert "claim_pending_task" not in getattr(dispatch, "__all__", [])


# ── (2) Redis-None startup guard ──────────────────────────────────────────────


async def _seed_executor_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Insert one active ``provider='executor'`` ModelAccount directly.

    Bypasses :class:`ModelAccountService` so the test doesn't need a real
    KMS-derived :class:`CredentialCipher` — executor accounts carry no api_key
    anyway (the ``api_key_encrypted`` column was made nullable in lift 5a).
    """
    async with sf() as session:
        session.add(
            ModelAccount(
                workspace_id=workspace_id,
                account_id=account_id,
                provider="executor",
                label="claude-code-worker",
                litellm_model="executor/claude_code",
                api_base=None,
                api_key_encrypted=None,
                data_jurisdiction="us-1",
                is_active=True,
                extra_params={"executor_type": "claude_code"},
            )
        )
        await session.commit()


async def test_health_warns_when_executor_accounts_active_but_no_redis(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Active executor account + empty redis_url ⇒ structured WARNING; no crash."""
    await _seed_executor_account(sf, workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    captured: list[dict[str, object]] = []
    structlog.configure(
        processors=[lambda _logger, _name, event_dict: captured.append(event_dict) or ""],
    )
    try:
        result = await check_executor_dispatch_health(session_factory=sf, redis_url="")
    finally:
        structlog.reset_defaults()
    assert result["healthy"] is False
    assert result["executor_account_count"] >= 1
    assert result["redis_configured"] is False
    events = [e.get("event") for e in captured]
    assert "executor_dispatch_no_redis" in events
    # Hint must point operators at the env var.
    warn = next(e for e in captured if e.get("event") == "executor_dispatch_no_redis")
    assert "BSVIBE_REDIS_URL" in str(warn)


async def test_health_quiet_when_redis_configured(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Executor accounts + redis_url set ⇒ no warning event emitted."""
    await _seed_executor_account(sf, workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    captured: list[dict[str, object]] = []
    structlog.configure(
        processors=[lambda _logger, _name, event_dict: captured.append(event_dict) or ""],
    )
    try:
        result = await check_executor_dispatch_health(
            session_factory=sf, redis_url="redis://localhost:6387/0"
        )
    finally:
        structlog.reset_defaults()
    assert result["healthy"] is True
    assert result["executor_account_count"] >= 1
    assert result["redis_configured"] is True
    assert "executor_dispatch_no_redis" not in [e.get("event") for e in captured]


async def test_health_quiet_when_no_executor_accounts(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """No executor accounts ⇒ no warning even when redis_url is empty."""
    captured: list[dict[str, object]] = []
    structlog.configure(
        processors=[lambda _logger, _name, event_dict: captured.append(event_dict) or ""],
    )
    try:
        result = await check_executor_dispatch_health(session_factory=sf, redis_url="")
    finally:
        structlog.reset_defaults()
    assert result["healthy"] is True
    assert result["executor_account_count"] == 0
    assert "executor_dispatch_no_redis" not in [e.get("event") for e in captured]


# ── (3) Operator templates exist ──────────────────────────────────────────────


def test_launchd_plist_template_present() -> None:
    here = Path(__file__).resolve().parents[2]
    plist = here / "backend/executors/worker/launchd/com.bsvibe.worker.plist.example"
    assert plist.is_file(), f"missing launchd template: {plist}"
    text = plist.read_text(encoding="utf-8")
    # Sanity: it parameterises the module entry + env vars + log paths.
    assert "backend.executors.worker" in text
    assert "BSVIBE_WORKER_SERVER_URL" in text
    assert "StandardOutPath" in text


def test_systemd_unit_template_present() -> None:
    here = Path(__file__).resolve().parents[2]
    unit = here / "backend/executors/worker/systemd/bsvibe-worker.service.example"
    assert unit.is_file(), f"missing systemd template: {unit}"
    text = unit.read_text(encoding="utf-8")
    assert "[Service]" in text
    assert "backend.executors.worker" in text
