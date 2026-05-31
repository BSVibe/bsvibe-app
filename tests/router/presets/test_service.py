"""PresetService.apply_preset — atomic CRUD + idempotency + audit emit."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.router.embedding.repository import IntentRepository
from backend.router.embedding.service import EmbeddedExample
from backend.router.presets.models import ModelMapping
from backend.router.presets.service import PresetService
from backend.router.routing.catalog_repository import ModelCatalogRepository
from backend.router.rules.repository import RulesRepository


class _FakeEmbeddingService:
    model = "ollama/nomic-embed-text"

    async def embed_many(self, texts: list[str]) -> list[EmbeddedExample]:
        return [EmbeddedExample(text=t, embedding=[0.1, 0.2, 0.3], model=self.model) for t in texts]


class _FakeAuditEmitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event, *, session) -> None:  # noqa: ANN001 — duck-typed
        self.events.append(event)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


async def _register_models(session, workspace_id, account_id, model_names: list[str]) -> None:
    """Seed the per-account catalog with concrete model names so apply()
    passes the "registered models" validation."""
    repo = ModelCatalogRepository(session)
    for name in model_names:
        await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            origin="custom",
            litellm_model=f"litellm-{name}",
            litellm_params=None,
            is_passthrough=True,
        )


def _mapping() -> ModelMapping:
    return ModelMapping(economy="econ", balanced="bal", premium="prem")


class TestApply:
    async def test_creates_intents_examples_and_rules(self, session, workspace_id, account_id):
        await _register_models(session, workspace_id, account_id, ["econ", "bal", "prem"])
        audit = _FakeAuditEmitter()
        svc = PresetService(
            session=session,
            audit_emitter=audit,
            actor_id="user:tester",
        )
        result = await svc.apply_preset(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_name="coding-assistant",
            model_mapping=_mapping(),
            embedding_service=_FakeEmbeddingService(),
        )
        assert result.preset_name == "coding-assistant"
        assert result.intents_created > 0
        assert result.rules_created > 0
        assert result.examples_created > 0

        # Verify rules + intents materialized.
        rules = await RulesRepository(session).list_rules(
            workspace_id=workspace_id, account_id=account_id
        )
        intents = await IntentRepository(session).list_intents(
            workspace_id=workspace_id, account_id=account_id
        )
        assert len(rules) == result.rules_created
        assert len(intents) == result.intents_created

        # Default rule present + non-default rule too.
        assert any(r.is_default for r in rules)

    async def test_emits_audit_event_on_success(self, session, workspace_id, account_id):
        await _register_models(session, workspace_id, account_id, ["econ", "bal", "prem"])
        audit = _FakeAuditEmitter()
        svc = PresetService(
            session=session,
            audit_emitter=audit,
            actor_id="user:tester",
        )
        await svc.apply_preset(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_name="coding-assistant",
            model_mapping=_mapping(),
            embedding_service=None,
        )
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event.event_type == "gateway.preset.applied"
        assert event.data["preset_name"] == "coding-assistant"
        assert str(workspace_id) == event.workspace_id


class TestValidation:
    async def test_rejects_unknown_preset(self, session, workspace_id, account_id):
        await _register_models(session, workspace_id, account_id, ["econ", "bal", "prem"])
        svc = PresetService(
            session=session,
            audit_emitter=_FakeAuditEmitter(),
            actor_id="user:tester",
        )
        with pytest.raises(ValueError, match="Unknown preset"):
            await svc.apply_preset(
                workspace_id=workspace_id,
                account_id=account_id,
                preset_name="nope",
                model_mapping=_mapping(),
                embedding_service=None,
            )

    async def test_rejects_unregistered_models(self, session, workspace_id, account_id):
        # No catalog entries → resolve() will produce model names that aren't registered.
        svc = PresetService(
            session=session,
            audit_emitter=_FakeAuditEmitter(),
            actor_id="user:tester",
        )
        with pytest.raises(ValueError, match="not registered"):
            await svc.apply_preset(
                workspace_id=workspace_id,
                account_id=account_id,
                preset_name="general",
                model_mapping=_mapping(),
                embedding_service=None,
            )


class TestIdempotency:
    async def test_second_apply_rejected_when_intents_overlap(
        self, session, workspace_id, account_id
    ):
        await _register_models(session, workspace_id, account_id, ["econ", "bal", "prem"])
        audit = _FakeAuditEmitter()
        svc = PresetService(
            session=session,
            audit_emitter=audit,
            actor_id="user:tester",
        )
        await svc.apply_preset(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_name="coding-assistant",
            model_mapping=_mapping(),
            embedding_service=None,
        )
        with pytest.raises(ValueError, match="already applied"):
            await svc.apply_preset(
                workspace_id=workspace_id,
                account_id=account_id,
                preset_name="coding-assistant",
                model_mapping=_mapping(),
                embedding_service=None,
            )
        # Audit captures only the first apply.
        assert len(audit.events) == 1


class TestPriorityCollision:
    async def test_base_priority_above_existing(self, session, workspace_id, account_id):
        await _register_models(session, workspace_id, account_id, ["econ", "bal", "prem"])
        # Seed a manual rule at priority=1 so the preset must start above it.
        rules_repo = RulesRepository(session)
        await rules_repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="manual",
            priority=1,
            target_model="econ",
        )
        svc = PresetService(
            session=session,
            audit_emitter=_FakeAuditEmitter(),
            actor_id="user:tester",
        )
        await svc.apply_preset(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_name="general",
            model_mapping=_mapping(),
            embedding_service=None,
        )
        rules = await rules_repo.list_rules(workspace_id=workspace_id, account_id=account_id)
        # All preset rules should land at priority > 1, no UNIQUE violation.
        preset_rules = [r for r in rules if r.name != "manual"]
        assert all(r.priority > 1 for r in preset_rules)


class TestEmbeddingGracefulDegrade:
    async def test_examples_inserted_without_embedding_when_service_missing(
        self, session, workspace_id, account_id
    ):
        await _register_models(session, workspace_id, account_id, ["econ", "bal", "prem"])
        svc = PresetService(
            session=session,
            audit_emitter=_FakeAuditEmitter(),
            actor_id="user:tester",
        )
        await svc.apply_preset(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_name="coding-assistant",
            model_mapping=_mapping(),
            embedding_service=None,
        )
        # Examples exist but with no embedding.
        intent_repo = IntentRepository(session)
        examples = await intent_repo.list_examples(workspace_id=workspace_id, account_id=account_id)
        assert examples  # at least one
        assert all(e.embedding is None for e in examples)
