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

# Importing the module db registers the tables on the shared Base.metadata so
# ``memory_session``'s create_all materialises them.
import backend.executors.db  # noqa: F401
from backend.executors import service
from backend.executors.db import WorkerRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio


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
