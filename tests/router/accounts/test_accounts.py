"""Tests for ModelAccount CRUD + KMS round-trip."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from backend.router.accounts.crypto import CredentialCipher
from backend.router.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountUpdate,
)
from backend.router.accounts.service import ModelAccountService


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

    def test_jurisdiction_optional_defaults_to_unknown(self):
        # The founder no longer hand-picks a data jurisdiction; omitting it
        # must succeed and fall back to the invisible-infra default.
        payload = ModelAccountCreate(
            provider="openai",
            label="x",
            litellm_model="openai/gpt-4o",
            api_key="x",
        )
        assert payload.data_jurisdiction == "unknown"

    async def test_create_without_jurisdiction_stores_unknown(
        self, service, workspace_id, account_id
    ):
        out = await service.create(
            workspace_id=workspace_id,
            account_id=account_id,
            payload=ModelAccountCreate(
                provider="openai",
                label="defaulted",
                litellm_model="openai/gpt-4o",
                api_key="sk-x",
            ),
        )
        assert out.data_jurisdiction == "unknown"

    async def test_create_with_explicit_jurisdiction_stores_it(
        self, service, workspace_id, account_id
    ):
        out = await service.create(
            workspace_id=workspace_id,
            account_id=account_id,
            payload=ModelAccountCreate(
                provider="openai",
                label="explicit-eu",
                litellm_model="openai/gpt-4o",
                api_key="sk-x",
                data_jurisdiction="eu",
            ),
        )
        assert out.data_jurisdiction == "eu"


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


class TestExecutorRowsHiddenFromList:
    """Lift 5a: provider=executor rows are routable accounts but must NOT
    appear in the api-llm Models list (the PWA shows workers separately)."""

    async def _seed_executor(self, service, workspace_id, account_id, *, label):
        # Executor rows are inserted via the low-level repo (no api_key to
        # encrypt). The encrypting ``create`` path is never used for them.
        return await service._repo.create(  # noqa: SLF001
            workspace_id=workspace_id,
            account_id=account_id,
            provider="executor",
            label=label,
            litellm_model="executor/claude_code",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={"worker_id": str(uuid.uuid4()), "executor_type": "claude_code"},
        )

    async def test_list_excludes_executor_rows(self, service, workspace_id, account_id):
        await service.create(
            workspace_id=workspace_id, account_id=account_id, payload=_make_create()
        )
        await self._seed_executor(service, workspace_id, account_id, label="laptop-1")
        rows = await service.list_(workspace_id=workspace_id, account_id=account_id)
        # Only the real LLM account is returned; the executor row is hidden.
        assert len(rows) == 1
        assert rows[0].provider == "openai"

    async def test_list_only_active_also_excludes_executor_rows(
        self, service, workspace_id, account_id
    ):
        await self._seed_executor(service, workspace_id, account_id, label="laptop-1")
        rows = await service.list_(
            workspace_id=workspace_id, account_id=account_id, only_active=True
        )
        assert rows == []

    async def test_get_executor_row_still_resolves(self, service, workspace_id, account_id):
        # Lift 5b resolution fetches an executor account by id directly — get
        # must keep working even though it's hidden from the list.
        row = await self._seed_executor(service, workspace_id, account_id, label="laptop-1")
        fetched = await service.get(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=row.id,
        )
        assert fetched is not None
        assert fetched.provider == "executor"
        assert fetched.has_api_key is False


class TestRevealApiKeyLocalProviders:
    """Local-inference providers (Ollama / LM Studio / llama.cpp / vLLM) run
    on the operator's host — their ``api_key_encrypted`` is meaningless and
    may legitimately be NULL. ``reveal_api_key`` returns the empty string for
    them rather than raising. Every other provider still rejects a NULL key
    so an accidentally-incomplete row never silently dispatches."""

    async def _seed_provider(
        self, service, workspace_id, account_id, *, provider: str, api_key_encrypted=None
    ):
        return await service._repo.create(  # noqa: SLF001
            workspace_id=workspace_id,
            account_id=account_id,
            provider=provider,
            label=f"{provider}-seed",
            litellm_model=f"{provider}/whatever",
            api_base=None,
            api_key_encrypted=api_key_encrypted,
            data_jurisdiction="self-hosted-kr",
            extra_params={},
        )

    @pytest.mark.parametrize("provider", ["ollama", "lmstudio", "llama_cpp", "vllm"])
    async def test_local_provider_null_key_returns_empty(
        self, service, workspace_id, account_id, provider
    ):
        row = await self._seed_provider(
            service, workspace_id, account_id, provider=provider, api_key_encrypted=None
        )
        # NULL is allowed → empty string, no raise.
        assert service.reveal_api_key(row) == ""

    async def test_local_provider_populated_key_still_decrypts(
        self, service, workspace_id, account_id, cipher
    ):
        encrypted = cipher.encrypt("sk-ollama")
        row = await self._seed_provider(
            service,
            workspace_id,
            account_id,
            provider="ollama",
            api_key_encrypted=encrypted,
        )
        # If the operator does set a key (rare), it still round-trips.
        assert service.reveal_api_key(row) == "sk-ollama"

    async def test_non_local_provider_null_key_still_raises(
        self, service, workspace_id, account_id
    ):
        row = await self._seed_provider(
            service,
            workspace_id,
            account_id,
            provider="openai",
            api_key_encrypted=None,
        )
        with pytest.raises(ValueError, match="has no api key to reveal"):
            service.reveal_api_key(row)
