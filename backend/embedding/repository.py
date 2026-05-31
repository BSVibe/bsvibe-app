"""SQL CRUD for intent definitions / examples + per-account embedding settings."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.embedding.db import (
    AccountEmbeddingSettingsRow,
    IntentDefinitionRow,
    IntentExampleRow,
)
from backend.embedding.settings import EmbeddingSettings


class IntentDuplicateError(Exception):
    """Raised on ``(workspace_id, account_id, name)`` collision."""


class IntentRepository:
    """CRUD for ``intent_definitions`` + ``intent_examples``, account-scoped."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_intent(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        name: str,
        description: str = "",
        threshold: float = 0.65,
    ) -> IntentDefinitionRow:
        row = IntentDefinitionRow(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            description=description,
            threshold=threshold,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise IntentDuplicateError(str(exc.orig)) from exc
        return row

    async def list_intents(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Sequence[IntentDefinitionRow]:
        stmt = select(IntentDefinitionRow).where(
            IntentDefinitionRow.workspace_id == workspace_id,
            IntentDefinitionRow.account_id == account_id,
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def delete_intent(
        self,
        intent_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> bool:
        stmt = select(IntentDefinitionRow).where(
            IntentDefinitionRow.id == intent_id,
            IntentDefinitionRow.workspace_id == workspace_id,
            IntentDefinitionRow.account_id == account_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    # ----- examples -----

    async def add_example(
        self,
        *,
        intent_id: uuid.UUID,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        text: str,
        embedding: list[float] | None,
        embedding_model: str | None,
    ) -> IntentExampleRow:
        row = IntentExampleRow(
            intent_id=intent_id,
            workspace_id=workspace_id,
            account_id=account_id,
            text=text,
            embedding=embedding,
            embedding_model=embedding_model,
            dimension=len(embedding) if embedding else None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_examples(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        embedding_model: str | None = None,
    ) -> Sequence[IntentExampleRow]:
        """List examples scoped by account, optionally filtering by model.

        ``embedding_model=None`` returns every example (including ones
        with no embedding yet). Passing a value restricts to rows whose
        embedding matches that model — the standard hot-path read used
        by :class:`IntentClassifier`.
        """
        clauses = [
            IntentExampleRow.workspace_id == workspace_id,
            IntentExampleRow.account_id == account_id,
        ]
        if embedding_model is not None:
            clauses.append(IntentExampleRow.embedding_model == embedding_model)
        stmt = select(IntentExampleRow).where(*clauses)
        return (await self._session.execute(stmt)).scalars().all()

    async def list_examples_needing_reembedding(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        active_model: str,
    ) -> Sequence[IntentExampleRow]:
        """Rows whose embedding is missing or stamped with a different model."""
        stmt = select(IntentExampleRow).where(
            IntentExampleRow.workspace_id == workspace_id,
            IntentExampleRow.account_id == account_id,
            or_(
                IntentExampleRow.embedding.is_(None),
                IntentExampleRow.embedding_model.is_(None),
                IntentExampleRow.embedding_model != active_model,
            ),
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def update_example_embedding(
        self,
        example_id: uuid.UUID,
        *,
        embedding: list[float],
        embedding_model: str,
    ) -> None:
        stmt = select(IntentExampleRow).where(IntentExampleRow.id == example_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.embedding = embedding
        row.embedding_model = embedding_model
        row.dimension = len(embedding)
        await self._session.flush()


class EmbeddingSettingsRepository:
    """Upsert / get per-account :class:`EmbeddingSettings`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        settings: EmbeddingSettings,
    ) -> AccountEmbeddingSettingsRow:
        existing = await self._row(workspace_id=workspace_id, account_id=account_id)
        config = {"embedding": settings.to_dict()}
        if existing is not None:
            existing.config = config
            await self._session.flush()
            return existing
        row = AccountEmbeddingSettingsRow(
            workspace_id=workspace_id,
            account_id=account_id,
            config=config,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> EmbeddingSettings | None:
        row = await self._row(workspace_id=workspace_id, account_id=account_id)
        if row is None:
            return None
        return EmbeddingSettings.from_account_settings(row.config)

    async def _row(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> AccountEmbeddingSettingsRow | None:
        stmt = select(AccountEmbeddingSettingsRow).where(
            AccountEmbeddingSettingsRow.workspace_id == workspace_id,
            AccountEmbeddingSettingsRow.account_id == account_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
