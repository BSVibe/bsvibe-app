"""Intent authoring application service (NL-native routing Lift N2).

The founder defines semantic categories ("marketing", "design",
"complex-coding") with a few example phrases; the examples get embedded so the
N1 :class:`~backend.router.routing.run_routing.intent_classifier.IntentClassifier`
can match incoming work against them.

This module is the ONE place both the REST endpoint (``/api/v1/intents``) and
the MCP tools (``bsvibe_intents_*``) call, so the "create def + embed examples"
logic and its failure modes live in a single, unit-testable seam. The embedder
is INJECTED (an ``embed_one``-shaped object) so tests never touch a real
embedding API — pass a stub, or ``None`` to exercise the no-embedding-config
path.

No-embedding-config behaviour (deliberate): when the account has no embedding
model configured, the intent + its examples are STILL created, each example
stamped ``embedding=None``. Nothing is lost — the examples surface via
:meth:`IntentRepository.list_examples_needing_reembedding` and the classifier
simply won't match this intent until embeddings exist. Authoring never
hard-fails on missing embedding config.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from backend.embedding.repository import IntentDuplicateError, IntentRepository
from backend.embedding.service import EmbeddedExample

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.embedding.db import IntentDefinitionRow

logger = structlog.get_logger(__name__)


class IntentAuthoringDuplicateError(Exception):
    """Raised when ``(workspace_id, account_id, name)`` already exists."""


class IntentNotFoundError(Exception):
    """Raised when deleting an intent that is not in the (workspace, account)."""


@runtime_checkable
class ExampleEmbedder(Protocol):
    """The ``embed_one`` + ``model`` slice of
    :class:`~backend.embedding.service.EmbeddingService` — kept as a Protocol so
    tests inject a deterministic stub and never hit a real provider."""

    @property
    def model(self) -> str: ...

    async def embed_one(self, text: str) -> EmbeddedExample: ...


async def build_account_embedder(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
) -> ExampleEmbedder | None:
    """Build the account's :class:`EmbeddingService`, or ``None``.

    ``None`` when the account has no embedding model configured — the caller
    then creates examples with ``embedding=None`` (see module docstring).
    Mirrors the construction in
    :func:`backend.router.routing.run_routing.intent_classifier.build_intent_classifier`.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from backend.embedding.db import AccountEmbeddingSettingsRow  # noqa: PLC0415
    from backend.embedding.provider import LiteLLMEmbeddingProvider  # noqa: PLC0415
    from backend.embedding.service import EmbeddingService  # noqa: PLC0415
    from backend.embedding.settings import EmbeddingSettings  # noqa: PLC0415

    config = await session.scalar(
        select(AccountEmbeddingSettingsRow.config).where(
            AccountEmbeddingSettingsRow.workspace_id == workspace_id,
            AccountEmbeddingSettingsRow.account_id == account_id,
        )
    )
    emb_settings = EmbeddingSettings.from_account_settings(config)
    if emb_settings is None:
        return None
    return EmbeddingService(LiteLLMEmbeddingProvider(emb_settings))


async def create_intent_with_examples(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    threshold: float,
    examples: list[str],
    embedder: ExampleEmbedder | None,
) -> IntentDefinitionRow:
    """Create an intent definition + its seed example rows.

    Each example is embedded via ``embedder`` (when provided) and the vector is
    stored on the example row (the same column the classifier searches). When
    ``embedder`` is ``None`` — or a provider hiccup yields ``embedding=None`` —
    the example is still persisted with ``embedding=None`` so nothing is lost.

    The transaction boundary is the CALLER's: this flushes but does not commit.
    Raises :class:`IntentAuthoringDuplicateError` on a name collision.
    """
    repo = IntentRepository(session)
    try:
        intent = await repo.create_intent(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            threshold=threshold,
        )
    except IntentDuplicateError as exc:
        raise IntentAuthoringDuplicateError(str(exc)) from exc

    embedded_count = 0
    for text in examples:
        embedding: list[float] | None = None
        embedding_model: str | None = None
        if embedder is not None:
            result = await embedder.embed_one(text)
            embedding = result.embedding
            # Only stamp the model when a vector actually came back — a None
            # embedding must stay re-embeddable (embedding_model None).
            embedding_model = result.model if result.embedding is not None else None
        await repo.add_example(
            intent_id=intent.id,
            workspace_id=workspace_id,
            account_id=account_id,
            text=text,
            embedding=embedding,
            embedding_model=embedding_model,
        )
        if embedding is not None:
            embedded_count += 1

    logger.info(
        "intent.authored",
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        name=name,
        examples=len(examples),
        embedded=embedded_count,
    )
    return intent


async def delete_intent(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Delete an intent (its examples + vectors cascade via the ORM relation).

    Raises :class:`IntentNotFoundError` when the id is not in the (workspace,
    account). Flushes but does not commit — the caller owns the transaction.
    """
    repo = IntentRepository(session)
    deleted = await repo.delete_intent(intent_id, workspace_id=workspace_id, account_id=account_id)
    if not deleted:
        raise IntentNotFoundError(str(intent_id))
    logger.info(
        "intent.deleted",
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        intent_id=str(intent_id),
    )


__all__ = [
    "ExampleEmbedder",
    "IntentAuthoringDuplicateError",
    "IntentNotFoundError",
    "build_account_embedder",
    "create_intent_with_examples",
    "delete_intent",
]
