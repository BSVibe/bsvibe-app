"""분류기·authoring 이 배포 임베딩 모델을 쓰는가 (PR A).

prod 실측 2026-08-24: ``account_embedding_settings`` **0행**, 그리고
``EmbeddingSettingsRepository.upsert`` 의 호출자는 **유닛테스트 하나뿐**이다 —
REST·MCP·PWA 어디에도 그 행을 쓰는 표면이 없다. 그래서 intent 는 만들어지지만
(``embedding=None``) 분류기는 항상 ``None`` 을 리턴하고 ``classified_intent``
룰은 영원히 발화하지 못한다.

바로 옆 knowledge 경로는 반대로 갔고 사유가 ``backend/config.py`` 에 적혀 있다:
*"knowledge search is not opt-in per workspace"*. 그 배포 모델
(``knowledge_embedding_model``, prod = ``ollama/bge-m3``) 을 임베딩 설정의
폴백으로 쓰면 새 사용자 설정 0개로 전원이 들어온다.

**전선의 양끝을 다 덮는다** — 받는 쪽(분류기)만 고치면 예시가 여전히
``embedding=None`` 으로 저장돼 매치가 안 된다.
"""

from __future__ import annotations

import uuid

import pytest

from backend.config import Settings
from backend.embedding.authoring import build_account_embedder, create_intent_with_examples
from backend.embedding.settings import resolve_embedding_settings
from backend.router.routing.run_routing.intent_classifier import build_intent_classifier


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


def _settings(model: str = "ollama/bge-m3") -> Settings:
    return Settings(
        knowledge_embedding_model=model,
        knowledge_embedding_api_base="http://host.docker.internal:11434",
        knowledge_embedding_timeout_s=30.0,
    )


class TestResolveEmbeddingSettings:
    """설정 해석 — 계정 설정이 이기고, 없으면 배포 모델, 둘 다 없으면 None."""

    def test_deployment_model_is_used_when_the_account_has_no_row(self) -> None:
        resolved = resolve_embedding_settings(None, _settings())
        assert resolved is not None
        assert resolved.model == "ollama/bge-m3"
        assert resolved.api_base == "http://host.docker.internal:11434"

    def test_account_config_wins_over_the_deployment_model(self) -> None:
        """양성 대조군 — 사용자 설정은 이 변경 전에도, 후에도 이긴다."""
        account_config = {"embedding": {"model": "text-embedding-3-small"}}
        resolved = resolve_embedding_settings(account_config, _settings())
        assert resolved is not None
        assert resolved.model == "text-embedding-3-small"

    def test_none_when_neither_is_configured(self) -> None:
        """음성 대조군 — 배포 모델을 비우면 폴백도 죽어야 한다."""
        assert resolve_embedding_settings(None, _settings(model="")) is None


class TestAuthoringSideOfTheWire:
    """주는 쪽 — 예시가 실제 벡터로 저장되는가."""

    async def test_embedder_is_built_from_the_deployment_model(
        self, session, workspace_id, account_id
    ) -> None:
        embedder = await build_account_embedder(
            session,
            settings=_settings(),
            workspace_id=workspace_id,
            account_id=account_id,
        )
        assert embedder is not None
        assert embedder.model == "ollama/bge-m3"

    async def test_no_embedder_when_the_wire_is_cut(
        self, session, workspace_id, account_id
    ) -> None:
        """음성 대조군 — 배포 모델을 비우면 None 으로 돌아간다."""
        embedder = await build_account_embedder(
            session,
            settings=_settings(model=""),
            workspace_id=workspace_id,
            account_id=account_id,
        )
        assert embedder is None


class TestClassifierSideOfTheWire:
    """받는 쪽 — 분류기가 실제로 만들어지는가."""

    async def test_classifier_is_built_from_the_deployment_model(
        self, session, workspace_id, account_id
    ) -> None:
        embedder = await build_account_embedder(
            session,
            settings=_settings(),
            workspace_id=workspace_id,
            account_id=account_id,
        )
        await create_intent_with_examples(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            name="architecture_design",
            threshold=0.65,
            examples=["리팩터링 방향을 잡아줘"],
            embedder=embedder,
        )
        classifier = await build_intent_classifier(
            session, _settings(), workspace_id=workspace_id, account_id=account_id
        )
        assert classifier is not None

    async def test_no_classifier_when_the_wire_is_cut(
        self, session, workspace_id, account_id
    ) -> None:
        """음성 대조군 — 배포 모델이 없으면 intent 가 있어도 None."""
        await create_intent_with_examples(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            name="architecture_design",
            threshold=0.65,
            examples=["리팩터링 방향을 잡아줘"],
            embedder=None,
        )
        classifier = await build_intent_classifier(
            session, _settings(model=""), workspace_id=workspace_id, account_id=account_id
        )
        assert classifier is None

    async def test_no_classifier_without_intents(self, session, workspace_id, account_id) -> None:
        """양성 대조군 — intent 가 없으면 배포 모델이 있어도 None (기존 동작)."""
        classifier = await build_intent_classifier(
            session, _settings(), workspace_id=workspace_id, account_id=account_id
        )
        assert classifier is None
