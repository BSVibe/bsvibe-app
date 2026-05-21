"""Tests for ModelAccount CRUD + KMS round-trip."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from backend.accounts.crypto import CredentialCipher
from backend.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountUpdate,
)
from backend.accounts.service import ModelAccountService


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def service(session, cipher: CredentialCipher) -> ModelAccountService:
    return ModelAccountService(session, cipher=cipher)


def _make_create() -> ModelAccountCreate:
    return ModelAccountCreate(
        provider="openai",
        label="prod-gpt4o",
        litellm_model="openai/gpt-4o",
        api_base=None,
        api_key="sk-secret",
        data_jurisdiction="us",
    )


class TestCreate:
    async def test_creates_row_with_encrypted_key(self, service, workspace_id, account_id, cipher):
        out = await service.create(
            workspace_id=workspace_id,
            account_id=account_id,
            payload=_make_create(),
        )
        assert out.provider == "openai"
        assert out.label == "prod-gpt4o"
        assert out.has_api_key is True
        # ModelAccountOut never exposes the encrypted bytes, but we can
        # verify round-trip via reveal_api_key.
        row = await service._repo.get(  # noqa: SLF001
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=out.id,
        )
        assert row is not None
        assert service.reveal_api_key(row) == "sk-secret"

    async def test_duplicate_label_within_account_fails(self, service, workspace_id, account_id):
        from sqlalchemy.exc import IntegrityError

        payload = _make_create()
        await service.create(workspace_id=workspace_id, account_id=account_id, payload=payload)
        with pytest.raises(IntegrityError):
            await service.create(workspace_id=workspace_id, account_id=account_id, payload=payload)

    async def test_same_label_allowed_across_accounts(self, service, workspace_id):
        other_account = uuid.uuid4()
        a1 = uuid.uuid4()
        await service.create(workspace_id=workspace_id, account_id=a1, payload=_make_create())
        await service.create(
            workspace_id=workspace_id,
            account_id=other_account,
            payload=_make_create(),
        )

    def test_invalid_jurisdiction_rejected_at_schema(self):
        with pytest.raises(ValidationError):
            ModelAccountCreate(
                provider="openai",
                label="x",
                litellm_model="openai/gpt-4o",
                api_key="x",
                data_jurisdiction="mars",  # type: ignore[arg-type]
            )


class TestListGetUpdateDelete:
    async def test_list_returns_only_account_rows(self, service, workspace_id, account_id):
        await service.create(
            workspace_id=workspace_id, account_id=account_id, payload=_make_create()
        )
        rows = await service.list_(workspace_id=workspace_id, account_id=account_id)
        assert len(rows) == 1
        # An account I didn't create should yield empty.
        other = await service.list_(workspace_id=workspace_id, account_id=uuid.uuid4())
        assert other == []

    async def test_list_only_active(self, service, workspace_id, account_id):
        created = await service.create(
            workspace_id=workspace_id, account_id=account_id, payload=_make_create()
        )
        # Deactivate via update.
        await service.update(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=created.id,
            payload=ModelAccountUpdate(is_active=False),
        )
        only_active = await service.list_(
            workspace_id=workspace_id, account_id=account_id, only_active=True
        )
        assert only_active == []
        all_rows = await service.list_(workspace_id=workspace_id, account_id=account_id)
        assert len(all_rows) == 1
        assert all_rows[0].is_active is False

    async def test_get_returns_none_for_other_workspace(self, service, workspace_id, account_id):
        created = await service.create(
            workspace_id=workspace_id, account_id=account_id, payload=_make_create()
        )
        other_workspace = uuid.uuid4()
        result = await service.get(
            workspace_id=other_workspace,
            account_id=account_id,
            model_account_id=created.id,
        )
        assert result is None

    async def test_update_changes_api_key_encrypted_blob(
        self, service, workspace_id, account_id, cipher
    ):
        created = await service.create(
            workspace_id=workspace_id, account_id=account_id, payload=_make_create()
        )
        row_before = await service._repo.get(  # noqa: SLF001
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=created.id,
        )
        assert row_before is not None
        old_blob = row_before.api_key_encrypted

        await service.update(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=created.id,
            payload=ModelAccountUpdate(api_key="sk-rotated"),
        )
        row_after = await service._repo.get(  # noqa: SLF001
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=created.id,
        )
        assert row_after is not None
        assert row_after.api_key_encrypted != old_blob
        assert service.reveal_api_key(row_after) == "sk-rotated"

    async def test_update_unknown_returns_none(self, service, workspace_id, account_id):
        result = await service.update(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=uuid.uuid4(),
            payload=ModelAccountUpdate(label="new"),
        )
        assert result is None

    async def test_delete_returns_true_then_false(self, service, workspace_id, account_id):
        created = await service.create(
            workspace_id=workspace_id, account_id=account_id, payload=_make_create()
        )
        assert await service.delete(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=created.id,
        )
        assert not await service.delete(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=created.id,
        )
