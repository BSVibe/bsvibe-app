"""#1075 — re-registering a worker keeps the executor accounts the founder made.

Measured 2026-09-29: after worker ``mac-mini-e2e`` was re-registered (09-21),
every design/implement run stalled on account resolution. The founder had made
two claude_code accounts by hand (``litellm_model=opus`` / ``sonnet``), tagged
with the worker's id; revoking the old worker deleted EVERY executor account
bound to it, those two included, and registering the new one recreated only
``executor/<capability>``. The routing rules (``설계 → opus``, ``구현 → sonnet``)
and the workspace default (the sonnet account) pointed at nothing — no warning.

형님 ruled (2026-10-08): revoking deletes only the rows registration itself
creates (``executor/<capability>``); a hand-made row stays. When a worker of the
SAME name registers again, the hand-made rows of its dead predecessor are bound
to it — rules and defaults keep working.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import backend.executors.db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
import backend.router.accounts.models  # noqa: F401
from backend.executors import service
from backend.router.accounts.models import ModelAccount
from backend.router.infrastructure.repositories.model_account_repository_sql import (
    SqlAlchemyModelAccountRepository,
)

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _register(s, ws: uuid.UUID, name: str, caps: list[str]):
    worker, _ = await service.register_worker_for_workspace(
        s, workspace_id=ws, name=name, labels=[], capabilities=caps
    )
    await s.commit()
    return worker


async def _accounts(s, ws: uuid.UUID) -> list[ModelAccount]:
    rows = await s.execute(
        select(ModelAccount).where(
            ModelAccount.workspace_id == ws, ModelAccount.provider == "executor"
        )
    )
    return list(rows.scalars().all())


async def _hand_made(s, ws: uuid.UUID, worker_id: uuid.UUID, model: str) -> uuid.UUID:
    """What the founder created over MCP: a model-specific claude_code account."""
    auto = (await _accounts(s, ws))[0]
    row = await SqlAlchemyModelAccountRepository(s).create(
        workspace_id=ws,
        account_id=auto.account_id,
        provider="executor",
        label=f"claude {model}",
        litellm_model=model,
        api_base=None,
        api_key_encrypted=None,
        extra_params={"worker_id": str(worker_id), "executor_type": "claude_code"},
    )
    await s.commit()
    return row.id


async def test_revoking_keeps_a_hand_made_account() -> None:
    async with memory_session() as s:
        ws = uuid.uuid4()
        old = await _register(s, ws, "mac-mini-e2e", ["claude_code"])
        opus = await _hand_made(s, ws, old.id, "opus")

        await service.revoke_worker(s, workspace_id=ws, worker_id=old.id)
        await s.commit()

        left = {a.id for a in await _accounts(s, ws)}
        assert opus in left
        assert all(a.litellm_model != "executor/claude_code" for a in await _accounts(s, ws))


async def test_the_same_name_registering_again_takes_them_over() -> None:
    async with memory_session() as s:
        ws = uuid.uuid4()
        old = await _register(s, ws, "mac-mini-e2e", ["claude_code", "opencode"])
        opus = await _hand_made(s, ws, old.id, "opus")
        sonnet = await _hand_made(s, ws, old.id, "sonnet")
        await service.revoke_worker(s, workspace_id=ws, worker_id=old.id)
        await s.commit()

        new = await _register(s, ws, "mac-mini-e2e", ["claude_code", "opencode"])

        by_id = {a.id: a for a in await _accounts(s, ws)}
        assert by_id[opus].extra_params["worker_id"] == str(new.id)
        assert by_id[sonnet].extra_params["worker_id"] == str(new.id)
        # Registration's own rows: exactly one per capability, for the new worker.
        auto = sorted(
            a.litellm_model for a in by_id.values() if a.litellm_model.startswith("executor/")
        )
        assert auto == ["executor/claude_code", "executor/opencode"]


async def test_a_differently_named_worker_does_not_take_them() -> None:
    """Control — the name is the identity a re-registration carries."""
    async with memory_session() as s:
        ws = uuid.uuid4()
        old = await _register(s, ws, "mac-mini-e2e", ["claude_code"])
        opus = await _hand_made(s, ws, old.id, "opus")
        await service.revoke_worker(s, workspace_id=ws, worker_id=old.id)
        await s.commit()

        await _register(s, ws, "laptop", ["claude_code"])

        by_id = {a.id: a for a in await _accounts(s, ws)}
        assert by_id[opus].extra_params["worker_id"] == str(old.id)


async def test_a_worker_without_the_capability_does_not_take_them() -> None:
    async with memory_session() as s:
        ws = uuid.uuid4()
        old = await _register(s, ws, "mac-mini-e2e", ["claude_code"])
        opus = await _hand_made(s, ws, old.id, "opus")
        await service.revoke_worker(s, workspace_id=ws, worker_id=old.id)
        await s.commit()

        await _register(s, ws, "mac-mini-e2e", ["opencode"])

        by_id = {a.id: a for a in await _accounts(s, ws)}
        assert by_id[opus].extra_params["worker_id"] == str(old.id)


async def test_a_live_workers_accounts_are_never_taken() -> None:
    """Two active workers may share a name — only a DEAD worker's rows move."""
    async with memory_session() as s:
        ws = uuid.uuid4()
        first = await _register(s, ws, "mac-mini-e2e", ["claude_code"])
        opus = await _hand_made(s, ws, first.id, "opus")

        # Two capabilities so its auto rows' labels do not collide with the first's.
        await _register(s, ws, "mac-mini-e2e", ["claude_code", "opencode"])

        by_id = {a.id: a for a in await _accounts(s, ws)}
        assert by_id[opus].extra_params["worker_id"] == str(first.id)
