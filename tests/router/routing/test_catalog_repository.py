"""ModelCatalogRepository — CRUD on per-account model_catalog_entries."""

from __future__ import annotations

import uuid

import pytest

from backend.router.routing.catalog_repository import (
    ModelCatalogDuplicateError,
    ModelCatalogRepository,
)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


class TestCatalogCRUD:
    async def test_create_and_list(self, session, workspace_id, account_id):
        repo = ModelCatalogRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            name="my-claude",
            origin="custom",
            litellm_model="claude-3-5-sonnet",
            litellm_params={"max_tokens": 4096},
            is_passthrough=False,
        )
        rows = await repo.list_for_account(workspace_id=workspace_id, account_id=account_id)
        assert [r.name for r in rows] == ["my-claude"]
        assert rows[0].litellm_params == {"max_tokens": 4096}

    async def test_unique_name_per_account(self, session, workspace_id, account_id):
        repo = ModelCatalogRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            name="dup",
            origin="custom",
            litellm_model="x",
            litellm_params=None,
            is_passthrough=True,
        )
        with pytest.raises(ModelCatalogDuplicateError):
            await repo.create(
                workspace_id=workspace_id,
                account_id=account_id,
                name="dup",
                origin="custom",
                litellm_model="y",
                litellm_params=None,
                is_passthrough=True,
            )

    async def test_account_isolation(self, session, workspace_id):
        a = uuid.uuid4()
        b = uuid.uuid4()
        repo = ModelCatalogRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            account_id=a,
            name="shared",
            origin="custom",
            litellm_model="x",
            litellm_params=None,
            is_passthrough=True,
        )
        # Same name allowed in different account.
        await repo.create(
            workspace_id=workspace_id,
            account_id=b,
            name="shared",
            origin="custom",
            litellm_model="y",
            litellm_params=None,
            is_passthrough=True,
        )
        a_rows = await repo.list_for_account(workspace_id=workspace_id, account_id=a)
        b_rows = await repo.list_for_account(workspace_id=workspace_id, account_id=b)
        assert a_rows[0].litellm_model == "x"
        assert b_rows[0].litellm_model == "y"

    async def test_delete(self, session, workspace_id, account_id):
        repo = ModelCatalogRepository(session)
        row = await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            name="doomed",
            origin="custom",
            litellm_model="x",
            litellm_params=None,
            is_passthrough=True,
        )
        assert await repo.delete(row.id, workspace_id=workspace_id, account_id=account_id) is True
        assert await repo.list_for_account(workspace_id=workspace_id, account_id=account_id) == []


class TestOriginFilters:
    async def test_split_custom_and_hide_system(self, session, workspace_id, account_id):
        repo = ModelCatalogRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            name="my-model",
            origin="custom",
            litellm_model="x",
            litellm_params=None,
            is_passthrough=True,
        )
        await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            name="hidden-system-model",
            origin="hide_system",
            litellm_model=None,
            litellm_params=None,
            is_passthrough=False,
        )
        rows = await repo.list_for_account(workspace_id=workspace_id, account_id=account_id)
        by_origin = {r.origin: r for r in rows}
        assert {"custom", "hide_system"} == set(by_origin.keys())
