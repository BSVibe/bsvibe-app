"""L1 (#12) — Direct questions are answered first.

The founder rule: a Direct request that is a *question* (no concrete work
artifact, no build verb) should be classified ``knowledge_only`` and answered
directly, NEVER routed into the agent loop where it has nothing to build and
stands down with "couldn't complete". This must hold with OR without the cheap
frame LLM — the prod dogfood that died ("지금 프로젝트 상황 어때?") hit the
no-LLM keyword fallback, which historically forced ``agent_loop``.

These tests pin the deterministic question heuristic + the LLM-path answer-first
override. They also pin the inverse guards so a real build request never gets
silently answered instead of built.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.stages.frame import FrameConfig, FrameStage
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus


class _StubFrameLlm:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = json.dumps(response)

    async def complete_text(self, *, system: str, user: str) -> str:
        return self._response


def _loader(tmp_path: Path) -> SkillLoader:
    loader = SkillLoader(tmp_path)
    loader.load_all()
    return loader


def _request(payload: dict) -> RequestRow:
    return RequestRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        trigger_event_id=uuid.uuid4(),
        status=RequestStatus.OPEN,
        payload=payload,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


# --------------------------------------------------------------------------
# No-LLM keyword fallback — the path the prod dogfood question died on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_korean_question_no_llm_is_knowledge_only(tmp_path: Path) -> None:
    """The exact dogfood failure: a Korean status question with no frame LLM
    must be answered directly, not routed into the agent loop."""
    framed = await FrameStage().frame(
        request=_request({"text": "지금 프로젝트 상황 어때?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), default_artifact_type="direct_output"),
    )
    assert framed.path_classification == "knowledge_only"
    assert framed.pipeline == "single"


@pytest.mark.asyncio
async def test_english_question_no_llm_is_knowledge_only(tmp_path: Path) -> None:
    framed = await FrameStage().frame(
        request=_request({"text": "how's the project doing?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), default_artifact_type="direct_output"),
    )
    assert framed.path_classification == "knowledge_only"


@pytest.mark.asyncio
async def test_korean_how_question_with_noun_no_llm_is_knowledge_only(tmp_path: Path) -> None:
    """A question that mentions a build NOUN ("api") but no build verb is still a
    question — the noun must not push it into the loop (the old _BUILD_INTENT_WORDS
    set included nouns like 'api'/'system', which would have wrongly excluded it)."""
    framed = await FrameStage().frame(
        request=_request({"text": "이 api 어떻게 동작해?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), default_artifact_type="direct_output"),
    )
    assert framed.path_classification == "knowledge_only"


@pytest.mark.asyncio
async def test_build_request_as_question_no_llm_stays_agent_loop(tmp_path: Path) -> None:
    """A build request phrased as a question ("can you build ...?") carries a
    build verb → it is real work, must stay on the agent loop."""
    framed = await FrameStage().frame(
        request=_request({"text": "can you build a TTL cache module?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), default_artifact_type="direct_output"),
    )
    assert framed.path_classification == "agent_loop"


@pytest.mark.asyncio
async def test_non_question_statement_no_llm_stays_agent_loop(tmp_path: Path) -> None:
    """Regression: a non-question imperative keeps today's agent_loop behaviour."""
    framed = await FrameStage().frame(
        request=_request({"text": "Please create the weekly digest for this week"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), default_artifact_type="direct_output"),
    )
    assert framed.path_classification == "agent_loop"


@pytest.mark.asyncio
async def test_question_with_work_artifact_default_stays_agent_loop(tmp_path: Path) -> None:
    """If the default artifact_type is a concrete WORK type, a question shape does
    NOT override — producing an artifact is work (coherence with the existing
    knowledge_only guard)."""
    framed = await FrameStage().frame(
        request=_request({"text": "should the homepage use a hero image?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), default_artifact_type="page"),
    )
    assert framed.path_classification == "agent_loop"


# --------------------------------------------------------------------------
# LLM path — answer-first override even when the LLM routes to the loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_agent_loop_question_upgraded_to_knowledge_only(tmp_path: Path) -> None:
    """Even when the frame LLM misclassifies a clear question as agent_loop, a
    question with no work artifact is answered first (founder rule)."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "How is the project doing?",
            "skill_match": None,
            "artifact_type_hint": None,
            "path_classification": "agent_loop",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "지금 프로젝트 상황 어때?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.path_classification == "knowledge_only"


@pytest.mark.asyncio
async def test_llm_build_question_with_code_artifact_stays_agent_loop(tmp_path: Path) -> None:
    """A build-as-question that the LLM tags with a code artifact is real work —
    the work artifact wins over the answer-first override."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "Build a TTL cache.",
            "skill_match": None,
            "artifact_type_hint": "code",
            "path_classification": "agent_loop",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "can you build a TTL cache?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.path_classification == "agent_loop"
