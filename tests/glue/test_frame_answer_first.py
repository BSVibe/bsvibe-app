"""L1 (#12) — a founder's QUESTION is answered, never routed into the agent loop.

The classification (ASK vs PRODUCE) is the frame LLM's judgment. There is no
keyword heuristic: the old `?`/interrogative-cue/build-verb word lists were
English+Korean only, and a question phrased as a polite imperative slipped
through them. Prod run ff1615e8 (2026-07-13): "현 프로젝트 상황 설명해줘" was framed
``{artifact_type_hint: direct_output, path_classification: agent_loop}`` — the
executor ran with nothing to build and shipped an unrelated diff.

What this module pins:

* the LLM's verdict is honoured verbatim (no keyword override in either
  direction),
* the coherence guard survives — a concrete WORK artifact still beats a
  ``knowledge_only`` verdict (a request that produces something is work),
* when no verdict is obtainable (no frame LLM / call failed / unparseable) the
  stage raises :class:`FrameUnclassifiedError` — it must NEVER silently guess
  ``agent_loop`` and write code (no-implicit-routing).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.stages.frame import (
    FrameConfig,
    FrameStage,
    FrameUnclassifiedError,
)
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus


class _StubFrameLlm:
    def __init__(self, response: dict[str, Any] | str) -> None:
        self._response = response if isinstance(response, str) else json.dumps(response)

    async def complete_text(self, *, system: str, user: str) -> str:
        return self._response


class _FailingFrameLlm:
    async def complete_text(self, *, system: str, user: str) -> str:
        raise RuntimeError("model unavailable")


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
# The LLM decides — its verdict is honoured verbatim
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "현 프로젝트 상황 설명해줘",  # the prod failure — an ask phrased as an imperative
        "지금 프로젝트 상황 어때?",
        "explain the current project status",
        "プロジェクトの状況を教えて",  # a language no keyword list ever covered
    ],
)
@pytest.mark.asyncio
async def test_llm_knowledge_only_verdict_is_answered(tmp_path: Path, text: str) -> None:
    """Whatever the phrasing, mood, or language: the LLM says the founder wants to
    be TOLD something → the run is answered, not built."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "The founder wants an account of the project's state.",
            "skill_match": None,
            "artifact_type_hint": None,
            "path_classification": "knowledge_only",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": text}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.path_classification == "knowledge_only"
    assert framed.pipeline == "single"


@pytest.mark.asyncio
async def test_llm_agent_loop_verdict_is_built(tmp_path: Path) -> None:
    """The inverse: a real build stays on the loop."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "Build a TTL cache module.",
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


@pytest.mark.asyncio
async def test_no_keyword_override_of_the_llm_verdict(tmp_path: Path) -> None:
    """Regression guard for the deleted heuristic: a question-shaped text that the
    LLM classified as work stays work. The stage must not second-guess the verdict
    with word lists — that machinery is gone, and re-adding it would flip this."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "Investigate why the importer drops rows and fix it.",
            "skill_match": None,
            "artifact_type_hint": None,
            "path_classification": "agent_loop",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "임포터가 왜 행을 누락하지? 고쳐줘"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.path_classification == "agent_loop"


# --------------------------------------------------------------------------
# Coherence guard — a concrete WORK artifact beats a knowledge_only verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("artifact", ["code", "page", "page_image", "pr"])
@pytest.mark.asyncio
async def test_work_artifact_beats_knowledge_only_verdict(tmp_path: Path, artifact: str) -> None:
    """A verdict of ``knowledge_only`` paired with something to PRODUCE is
    incoherent — the artifact wins (prod 2026-05-28: "Create calc.py" was
    answered, shipped, and no file existed)."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "Document the dispatch layer in the README.",
            "skill_match": None,
            "artifact_type_hint": artifact,
            "path_classification": "knowledge_only",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "dispatch 레이어 README 에 설명해줘"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.path_classification == "agent_loop"


# --------------------------------------------------------------------------
# No verdict → explicit failure, never a silent guess (no-implicit-routing)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_frame_llm_raises_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(FrameUnclassifiedError):
        await FrameStage().frame(
            request=_request({"text": "현 프로젝트 상황 설명해줘"}),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=None),
        )


@pytest.mark.asyncio
async def test_frame_llm_failure_raises_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(FrameUnclassifiedError):
        await FrameStage().frame(
            request=_request({"text": "현 프로젝트 상황 설명해줘"}),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=_FailingFrameLlm()),
        )


@pytest.mark.asyncio
async def test_unparseable_frame_output_raises_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(FrameUnclassifiedError):
        await FrameStage().frame(
            request=_request({"text": "현 프로젝트 상황 설명해줘"}),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=_StubFrameLlm("no json here")),
        )


@pytest.mark.asyncio
async def test_missing_path_classification_raises_rather_than_guessing(tmp_path: Path) -> None:
    """The LLM answered, but not the question that decides the run's kind. Guessing
    ``agent_loop`` here is what shipped an unrelated diff in prod."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "The founder wants an account of the project's state.",
            "skill_match": None,
            "artifact_type_hint": None,
        }
    )
    with pytest.raises(FrameUnclassifiedError):
        await FrameStage().frame(
            request=_request({"text": "현 프로젝트 상황 설명해줘"}),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
        )
