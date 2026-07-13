"""L8 — the frame stage produces a short, plain-language ``summary_title``.

The founder's review surfaces (Decisions, Brief) showed the RAW Direction as the
task title — verbose, truncated, developer-y ("In the bsvibe-app product, add a
pure utility function `mean(values: list[float]) -> float` in backend/common/…").
The frame LLM already restates intent; this adds a SHORT plain-language title
(no paths / type sigs / code identifiers) the review rows lead with. No new LLM
call — it rides the existing single frame completion.
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


@pytest.mark.asyncio
async def test_llm_summary_title_is_captured(tmp_path: Path) -> None:
    llm = _StubFrameLlm(
        {
            "framed_intent": "Add a pure utility function to calculate the arithmetic mean.",
            "summary_title": "Add a mean helper",
            "skill_match": None,
            "artifact_type_hint": "code",
            "path_classification": "agent_loop",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "In the bsvibe-app product, add mean(values) in backend/common"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.summary_title == "Add a mean helper"


@pytest.mark.asyncio
async def test_summary_title_none_when_llm_omits_it(tmp_path: Path) -> None:
    llm = _StubFrameLlm(
        {
            "framed_intent": "Do the thing.",
            "skill_match": None,
            "artifact_type_hint": "code",
            "path_classification": "agent_loop",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "do the thing"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.summary_title is None


@pytest.mark.asyncio
async def test_summary_title_none_when_model_omits_it(tmp_path: Path) -> None:
    """The model omitted the key → no summary_title (the review surface falls back
    to the intent). Absent is not an error: only the KIND verdict is mandatory."""
    llm = _StubFrameLlm(
        {
            "framed_intent": "Add a weekly digest.",
            "skill_match": None,
            "artifact_type_hint": "code",
            "path_classification": "agent_loop",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "add a weekly digest"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.summary_title is None


@pytest.mark.asyncio
async def test_summary_title_blank_is_dropped(tmp_path: Path) -> None:
    llm = _StubFrameLlm(
        {
            "framed_intent": "X.",
            "summary_title": "   ",
            "skill_match": None,
            "artifact_type_hint": None,
            "path_classification": "knowledge_only",
        }
    )
    framed = await FrameStage().frame(
        request=_request({"text": "what is our deploy process?"}),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm),
    )
    assert framed.summary_title is None
