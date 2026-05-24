"""Service-layer tests for the external-worker registration subsystem.

Lift 1 of the executor-pool epic — the registration model ported from
BSGateway. These exercise :mod:`backend.executors.service` directly against an
in-memory SQLite session (the unit tier), with NO HTTP layer.

The subsystem lives under ``backend/executors/`` on its OWN tables
(``executor_workers`` / ``executor_install_tokens``) because the names
``workers`` / ``worker_install_tokens`` are already taken by the unrelated
Bundle G internal-daemon liveness model (``backend.workers.db``). Same axis
(``workspace_id``), distinct concept (external CLI executor pool).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

# Importing the module db registers the tables on the shared Base.metadata so
# ``memory_session``'s create_all materialises them.
import backend.accounts.account_models  # noqa: F401
import backend.accounts.models  # noqa: F401
import backend.executors.db  # noqa: F401
from backend.accounts.models import ModelAccount
from backend.executors import service
from backend.executors.db import WorkerRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _register(s, *, name="laptop-1", labels=None, capabilities=None):
    """Mint an install token + register a worker, returning ``(worker, token)``."""
    install = await service.mint_install_token(s, workspace_id=uuid.uuid4())
    await s.commit()
    # Resolve the workspace the install token belongs to so callers can scope.
    workspace_id = await service.resolve_install_token_workspace(s, install)
    assert workspace_id is not None
    worker, token = await service.register_worker(
        s,
        install_token=install,
        name=name,
        labels=labels or [],
        capabilities=capabilities if capabilities is not None else [],
    )
    await s.commit()
    return worker, token


async def _executor_accounts(s, workspace_id: uuid.UUID) -> list[ModelAccount]:
    rows = (
        (
            await s.execute(
                select(ModelAccount).where(
                    ModelAccount.workspace_id == workspace_id,
                    ModelAccount.provider == "executor",
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def test_mint_install_token_returns_plaintext_and_stores_hash() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        token = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        assert token and isinstance(token, str)
        # Plaintext is never persisted — only the SHA-256 hash.
        stored = await service.get_install_token_hash(s, workspace_id)
        assert stored == service._hash_token(token)
        assert stored != token


async def test_remint_replaces_prior_install_token() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        first = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        second = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        assert first != second
        # Only ONE active install token per workspace — the prior is gone.
        assert await service.resolve_install_token_workspace(s, first) is None
        assert await service.resolve_install_token_workspace(s, second) == workspace_id


async def test_register_worker_happy_path() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        install = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        worker, plaintext = await service.register_worker(
            s,
            install_token=install,
            name="laptop-1",
            labels=["mac"],
            capabilities=["claude_code", "codex"],
        )
        await s.commit()
        assert worker.workspace_id == workspace_id
        assert worker.name == "laptop-1"
        assert worker.labels == ["mac"]
        assert worker.capabilities == ["claude_code", "codex"]
        assert worker.status == "offline"
        assert worker.is_active is True
        # The plaintext worker token authenticates; the hash is what's stored.
        assert plaintext and worker.token_hash == service._hash_token(plaintext)
        assert worker.token_hash != plaintext


async def test_register_worker_rejects_bad_install_token() -> None:
    async with memory_session() as s:
        with pytest.raises(service.InvalidInstallToken):
            await service.register_worker(
                s,
                install_token="not-a-real-token",
                name="x",
                labels=[],
                capabilities=[],
            )


async def test_register_worker_rejects_absent_install_token() -> None:
    async with memory_session() as s:
        with pytest.raises(service.InvalidInstallToken):
            await service.register_worker(
                s,
                install_token="",
                name="x",
                labels=[],
                capabilities=[],
            )


async def test_authenticate_worker_round_trips() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        install = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        worker, plaintext = await service.register_worker(
            s, install_token=install, name="w", labels=[], capabilities=[]
        )
        await s.commit()
        authed = await service.authenticate_worker(s, plaintext)
        assert authed is not None
        assert authed.id == worker.id
        # A bogus token does not authenticate.
        assert await service.authenticate_worker(s, "garbage") is None
        # An empty token does not authenticate.
        assert await service.authenticate_worker(s, "") is None


async def test_record_heartbeat_sets_online_and_timestamp() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        install = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        worker, _ = await service.register_worker(
            s, install_token=install, name="w", labels=[], capabilities=[]
        )
        await s.commit()
        assert worker.last_heartbeat is None
        updated = await service.record_heartbeat(s, worker)
        await s.commit()
        assert updated.status == "online"
        assert updated.last_heartbeat is not None


async def test_list_workers_is_workspace_scoped() -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    async with memory_session() as s:
        token_a = await service.mint_install_token(s, workspace_id=ws_a)
        token_b = await service.mint_install_token(s, workspace_id=ws_b)
        await s.commit()
        await service.register_worker(
            s, install_token=token_a, name="a1", labels=[], capabilities=[]
        )
        await service.register_worker(
            s, install_token=token_a, name="a2", labels=[], capabilities=[]
        )
        await service.register_worker(
            s, install_token=token_b, name="b1", labels=[], capabilities=[]
        )
        await s.commit()
        a_workers = await service.list_workers(s, ws_a)
        b_workers = await service.list_workers(s, ws_b)
        assert {w.name for w in a_workers} == {"a1", "a2"}
        assert {w.name for w in b_workers} == {"b1"}


async def test_revoke_worker_makes_it_inactive_and_auth_fails() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        install = await service.mint_install_token(s, workspace_id=workspace_id)
        await s.commit()
        worker, plaintext = await service.register_worker(
            s, install_token=install, name="w", labels=[], capabilities=[]
        )
        await s.commit()
        revoked = await service.revoke_worker(s, workspace_id=workspace_id, worker_id=worker.id)
        await s.commit()
        assert revoked is not None
        assert revoked.is_active is False
        # An inactive worker can no longer authenticate.
        assert await service.authenticate_worker(s, plaintext) is None


async def test_revoke_worker_other_workspace_is_noop() -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    async with memory_session() as s:
        install = await service.mint_install_token(s, workspace_id=ws_a)
        await s.commit()
        worker, _ = await service.register_worker(
            s, install_token=install, name="w", labels=[], capabilities=[]
        )
        await s.commit()
        # ws_b cannot revoke ws_a's worker.
        result = await service.revoke_worker(s, workspace_id=ws_b, worker_id=worker.id)
        assert result is None
        # The worker remains active.
        still = await s.get(WorkerRow, worker.id)
        assert still is not None
        assert still.is_active is True


# ── Lift 5a: executor ModelAccount rows ───────────────────────────────────────


async def test_register_worker_creates_executor_model_accounts() -> None:
    async with memory_session() as s:
        worker, _ = await _register(s, name="laptop-1", capabilities=["claude_code", "codex"])
        rows = await _executor_accounts(s, worker.workspace_id)
        assert len(rows) == 2
        by_model = {r.litellm_model: r for r in rows}
        assert set(by_model) == {"executor/claude_code", "executor/codex"}
        for cap, row in (
            ("claude_code", by_model["executor/claude_code"]),
            ("codex", by_model["executor/codex"]),
        ):
            assert row.provider == "executor"
            # Multi-capability worker disambiguates the label with the capability.
            assert row.label == f"laptop-1 ({cap})"
            assert row.api_base is None
            # An executor account carries no api key — the column is now nullable.
            assert row.api_key_encrypted is None
            assert row.data_jurisdiction == "unknown"
            assert row.is_active is True
            assert row.extra_params == {
                "worker_id": str(worker.id),
                "executor_type": cap,
            }
        # All hang off the workspace's personal account.
        assert {r.account_id for r in rows} == {rows[0].account_id}


async def test_register_worker_single_capability_label_is_worker_name() -> None:
    async with memory_session() as s:
        worker, _ = await _register(s, name="solo", capabilities=["claude_code"])
        rows = await _executor_accounts(s, worker.workspace_id)
        assert len(rows) == 1
        assert rows[0].label == "solo"
        assert rows[0].litellm_model == "executor/claude_code"


async def test_register_worker_no_capabilities_creates_no_executor_accounts() -> None:
    async with memory_session() as s:
        worker, _ = await _register(s, name="bare", capabilities=[])
        rows = await _executor_accounts(s, worker.workspace_id)
        assert rows == []


async def test_register_worker_executor_accounts_are_idempotent() -> None:
    """Re-registering the same worker (same id) must not duplicate rows."""
    async with memory_session() as s:
        worker, _ = await _register(s, name="laptop-1", capabilities=["claude_code", "codex"])
        before = await _executor_accounts(s, worker.workspace_id)
        assert len(before) == 2
        # Re-upsert directly (simulates re-register / re-mint of the same worker).
        from backend.accounts.account_service import ensure_personal_account

        account = await ensure_personal_account(s, workspace_id=worker.workspace_id)
        await service._upsert_executor_model_accounts(
            s,
            workspace_id=worker.workspace_id,
            account_id=account.id,
            worker_id=worker.id,
            name="laptop-1",
            capabilities=["claude_code", "codex"],
        )
        await s.commit()
        after = await _executor_accounts(s, worker.workspace_id)
        assert {r.id for r in after} == {r.id for r in before}


async def test_revoke_worker_removes_executor_model_accounts() -> None:
    async with memory_session() as s:
        worker, _ = await _register(s, name="laptop-1", capabilities=["claude_code", "codex"])
        assert len(await _executor_accounts(s, worker.workspace_id)) == 2
        revoked = await service.revoke_worker(
            s, workspace_id=worker.workspace_id, worker_id=worker.id
        )
        await s.commit()
        assert revoked is not None
        assert revoked.is_active is False
        # The routable executor models are gone with the worker.
        assert await _executor_accounts(s, worker.workspace_id) == []


async def test_revoke_worker_only_removes_its_own_executor_accounts() -> None:
    async with memory_session() as s:
        a, _ = await _register(s, name="a", capabilities=["claude_code"])
        b, _ = await _register(s, name="b", capabilities=["codex"])
        # a and b live in different workspaces (each _register mints a new one),
        # so revoking a must leave b's account untouched.
        await service.revoke_worker(s, workspace_id=a.workspace_id, worker_id=a.id)
        await s.commit()
        assert await _executor_accounts(s, a.workspace_id) == []
        assert len(await _executor_accounts(s, b.workspace_id)) == 1
