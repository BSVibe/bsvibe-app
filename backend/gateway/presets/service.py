"""Apply a preset template to an account in one transaction.

Pipeline (single ``AsyncSession`` transaction):

1. Look up the named template via :class:`PresetRegistry`.
2. Validate ``target_level → concrete model`` for every preset rule
   against ``model_catalog_entries`` for the account. Reject if any
   resolved name isn't registered.
3. Reject if any preset intent name already exists for the account
   (idempotency: a re-apply would otherwise be silently destructive).
4. Pre-compute embeddings for every example text in one batch
   *outside* the rule/intent writes so we don't hold DB locks while
   waiting on the embedding API. Failure degrades to ``embedding=None``
   (rows still inserted; surface via ``list_examples_needing_reembedding``).
5. Insert intents → examples → rules → conditions through the existing
   1.5a/1.5b repositories. Rule priorities start above
   ``MAX(existing priority) + 1`` to avoid the deferred-unique trap.
6. Emit a :class:`PresetAppliedEvent` into the same session — atomic
   with the writes via the audit outbox.
"""

from __future__ import annotations

import uuid
from typing import Protocol

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.gateway.embedding.repository import IntentRepository
from backend.gateway.embedding.service import EmbeddingService
from backend.gateway.presets.events import PresetAppliedEvent
from backend.gateway.presets.models import (
    ModelMapping,
    PresetApplyResult,
    PresetIntent,
    PresetRule,
)
from backend.gateway.presets.registry import PresetRegistry
from backend.gateway.routing.catalog_repository import ModelCatalogRepository
from backend.gateway.rules.db import RoutingRuleRow
from backend.gateway.rules.repository import RulesRepository
from backend.supervisor.audit.events import AuditActor, AuditEventBase

logger = structlog.get_logger(__name__)


class AuditEmitterProtocol(Protocol):
    """Just the slice of :class:`backend.supervisor.audit.emitter.AuditEmitter`
    that :class:`PresetService` needs — kept as a Protocol so tests can
    inject a stub without touching the real outbox."""

    async def emit(self, event: AuditEventBase, *, session: AsyncSession) -> None: ...


class PresetService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit_emitter: AuditEmitterProtocol,
        actor_id: str,
        registry: PresetRegistry | None = None,
    ) -> None:
        self._session = session
        self._audit = audit_emitter
        self._actor_id = actor_id
        self._registry = registry or PresetRegistry()
        # Sibling repositories share the same session — every write
        # lands in the caller's transaction.
        self._rules = RulesRepository(session)
        self._intents = IntentRepository(session)
        self._catalog = ModelCatalogRepository(session)

    async def apply_preset(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        preset_name: str,
        model_mapping: ModelMapping,
        embedding_service: EmbeddingService | None,
    ) -> PresetApplyResult:
        preset = self._registry.get(preset_name)
        if preset is None:
            raise ValueError(f"Unknown preset: {preset_name}")

        await self._validate_models_registered(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_rules=preset.rules,
            model_mapping=model_mapping,
        )
        await self._validate_no_intent_collision(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_intent_names={i.name for i in preset.intents},
            preset_name=preset_name,
        )

        embedded = await self._embed_examples(
            preset_intents=preset.intents,
            embedding_service=embedding_service,
        )

        intents_created = 0
        examples_created = 0

        for intent_def in preset.intents:
            intent_row = await self._intents.create_intent(
                workspace_id=workspace_id,
                account_id=account_id,
                name=intent_def.name,
                description=intent_def.description,
                threshold=0.7,
            )
            intents_created += 1
            for example_text in intent_def.examples:
                embedding, model = embedded.get((intent_def.name, example_text), (None, None))
                await self._intents.add_example(
                    intent_id=intent_row.id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    text=example_text,
                    embedding=embedding,
                    embedding_model=model,
                )
                examples_created += 1

        base_priority = await self._next_priority(workspace_id=workspace_id, account_id=account_id)
        rules_created = 0
        for offset, rule_def in enumerate(preset.rules):
            concrete_model = model_mapping.resolve(rule_def.target_level)
            rule = await self._rules.create_rule(
                workspace_id=workspace_id,
                account_id=account_id,
                name=rule_def.name,
                priority=base_priority + offset,
                target_model=concrete_model,
                is_default=rule_def.is_default,
            )
            rules_created += 1
            if rule_def.conditions:
                await self._rules.replace_conditions(
                    rule.id,
                    [
                        {
                            "condition_type": c.condition_type,
                            "operator": c.operator,
                            "field": c.field,
                            "value": c.value,
                        }
                        for c in rule_def.conditions
                    ],
                )

        result = PresetApplyResult(
            preset_name=preset_name,
            rules_created=rules_created,
            intents_created=intents_created,
            examples_created=examples_created,
        )

        # ``event_type`` is filled by AuditEventBase.__init__ from
        # DEFAULT_EVENT_TYPE — pass it explicitly here so mypy
        # (which doesn't model the runtime override) is satisfied.
        await self._audit.emit(
            PresetAppliedEvent(
                event_type="gateway.preset.applied",
                actor=AuditActor(type="user", id=self._actor_id),
                workspace_id=str(workspace_id),
                data={
                    "preset_name": preset_name,
                    "account_id": str(account_id),
                    "rules_created": rules_created,
                    "intents_created": intents_created,
                    "examples_created": examples_created,
                },
            ),
            session=self._session,
        )

        logger.info(
            "preset.applied",
            workspace_id=str(workspace_id),
            account_id=str(account_id),
            preset=preset_name,
            rules=rules_created,
            intents=intents_created,
            examples=examples_created,
        )
        return result

    # ----- helpers -----

    async def _validate_models_registered(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        preset_rules: tuple[PresetRule, ...],
        model_mapping: ModelMapping,
    ) -> None:
        catalog = await self._catalog.list_for_account(
            workspace_id=workspace_id, account_id=account_id
        )
        registered = {row.name for row in catalog}
        for rule in preset_rules:
            concrete = model_mapping.resolve(rule.target_level)
            if concrete not in registered:
                raise ValueError(f"Model '{concrete}' is not registered for this account")

    async def _validate_no_intent_collision(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        preset_intent_names: set[str],
        preset_name: str,
    ) -> None:
        existing = await self._intents.list_intents(
            workspace_id=workspace_id, account_id=account_id
        )
        overlap = {i.name for i in existing} & preset_intent_names
        if overlap:
            raise ValueError(
                f"Preset '{preset_name}' appears already applied: "
                f"intents {sorted(overlap)} already exist"
            )

    async def _embed_examples(
        self,
        *,
        preset_intents: tuple[PresetIntent, ...],
        embedding_service: EmbeddingService | None,
    ) -> dict[tuple[str, str], tuple[list[float] | None, str | None]]:
        out: dict[tuple[str, str], tuple[list[float] | None, str | None]] = {}
        if embedding_service is None:
            return out
        flat: list[tuple[str, str]] = [
            (intent.name, ex) for intent in preset_intents for ex in intent.examples
        ]
        if not flat:
            return out
        results = await embedding_service.embed_many([text for _, text in flat])
        for (intent_name, text), result in zip(flat, results, strict=True):
            if result.embedding is None:
                out[(intent_name, text)] = (None, None)
            else:
                out[(intent_name, text)] = (result.embedding, result.model)
        return out

    async def _next_priority(self, *, workspace_id: uuid.UUID, account_id: uuid.UUID) -> int:
        stmt = select(func.max(RoutingRuleRow.priority)).where(
            RoutingRuleRow.workspace_id == workspace_id,
            RoutingRuleRow.account_id == account_id,
        )
        current = (await self._session.execute(stmt)).scalar_one_or_none()
        return (current or 0) + 1
