"""Lift I-Repo-Router — SqlAlchemyModelAccountRepository round-trip tests."""

from __future__ import annotations

import uuid

import pytest

from backend.router.infrastructure.repositories import SqlAlchemyModelAccountRepository
from tests._support import memory_session


@pytest.mark.asyncio
async def test_create_and_get_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        row = await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="local",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )

        got = await repo.get(workspace_id=ws, account_id=acc, model_account_id=row.id)
        assert got is not None
        assert got.id == row.id
        assert got.litellm_model == "ollama/qwen3"


@pytest.mark.asyncio
async def test_get_returns_none_when_cross_workspace() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        row = await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="local",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )

        other_ws = uuid.uuid4()
        got = await repo.get(workspace_id=other_ws, account_id=acc, model_account_id=row.id)
        assert got is None


@pytest.mark.asyncio
async def test_list_for_account_excludes_executor_provider() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        # native api-llm account
        await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="local",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )
        # executor-pool account (excluded by list_for_account)
        await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="executor",
            label="codex",
            litellm_model="executor/codex",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={"worker_id": str(uuid.uuid4()), "executor_type": "codex"},
        )

        rows = await repo.list_for_account(workspace_id=ws, account_id=acc)
        assert len(rows) == 1
        assert rows[0].provider == "ollama"


@pytest.mark.asyncio
async def test_list_for_account_only_active_filter() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        active = await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="local",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )
        inactive = await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="other",
            litellm_model="ollama/llama3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )
        inactive.is_active = False
        await session.flush()

        all_rows = await repo.list_for_account(workspace_id=ws, account_id=acc)
        assert {r.id for r in all_rows} == {active.id, inactive.id}

        active_only = await repo.list_for_account(workspace_id=ws, account_id=acc, only_active=True)
        assert {r.id for r in active_only} == {active.id}


@pytest.mark.asyncio
async def test_list_active_for_workspace_spans_accounts_and_filters_inactive() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()

        row_a = await repo.create(
            workspace_id=ws,
            account_id=acc_a,
            provider="ollama",
            label="A",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )
        row_b = await repo.create(
            workspace_id=ws,
            account_id=acc_b,
            provider="openai",
            label="B",
            litellm_model="gpt-4",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={},
        )
        inactive = await repo.create(
            workspace_id=ws,
            account_id=acc_a,
            provider="ollama",
            label="C",
            litellm_model="ollama/llama3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )
        inactive.is_active = False
        await session.flush()

        rows = await repo.list_active_for_workspace(workspace_id=ws)
        ids = {r.id for r in rows}
        assert row_a.id in ids
        assert row_b.id in ids
        assert inactive.id not in ids


@pytest.mark.asyncio
async def test_list_active_for_workspace_is_workspace_scoped() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        await repo.create(
            workspace_id=ws_a,
            account_id=uuid.uuid4(),
            provider="ollama",
            label="A",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )

        assert await repo.list_active_for_workspace(workspace_id=ws_b) == []


@pytest.mark.asyncio
async def test_list_executor_accounts_for_worker_matches_extra_params() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        worker_a = uuid.uuid4()
        worker_b = uuid.uuid4()

        await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="executor",
            label="codex-A",
            litellm_model="executor/codex",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={"worker_id": str(worker_a), "executor_type": "codex"},
        )
        await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="executor",
            label="claude-A",
            litellm_model="executor/claude_code",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={"worker_id": str(worker_a), "executor_type": "claude_code"},
        )
        await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="executor",
            label="codex-B",
            litellm_model="executor/codex",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={"worker_id": str(worker_b), "executor_type": "codex"},
        )

        rows = await repo.list_executor_accounts_for_worker(workspace_id=ws, worker_id=worker_a)
        labels = {r.label for r in rows}
        assert labels == {"codex-A", "claude-A"}


@pytest.mark.asyncio
async def test_delete_returns_true_then_false() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        row = await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="local",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )

        assert await repo.delete(workspace_id=ws, account_id=acc, model_account_id=row.id) is True
        assert await repo.delete(workspace_id=ws, account_id=acc, model_account_id=row.id) is False


@pytest.mark.asyncio
async def test_update_patches_non_none_fields_only() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyModelAccountRepository(session)
        ws = uuid.uuid4()
        acc = uuid.uuid4()
        row = await repo.create(
            workspace_id=ws,
            account_id=acc,
            provider="ollama",
            label="local",
            litellm_model="ollama/qwen3",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="local",
            extra_params={},
        )

        original_model = row.litellm_model
        updated = await repo.update(row, label="renamed", litellm_model=None)
        assert updated.label == "renamed"
        assert updated.litellm_model == original_model
