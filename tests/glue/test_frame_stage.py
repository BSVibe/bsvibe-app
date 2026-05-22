"""FrameStage — keyword skill match + artifact_type hint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.intake.db import RequestRow, RequestStatus
from backend.orchestrator.frame import FrameConfig, FrameStage
from backend.skills.loader import SkillLoader


def _write_skill(root: Path, name: str, description: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\nversion: 1\ndescription: {description}\n---\nbody",
        encoding="utf-8",
    )


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
async def test_skill_match_on_keyword(tmp_path: Path) -> None:
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from recent notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "Please create the weekly digest for this week"})
    framed = await FrameStage().frame(request=request, config=FrameConfig(skill_loader=loader))
    assert framed.skill_match == "weekly-digest"


@pytest.mark.asyncio
async def test_no_skill_match_returns_none(tmp_path: Path) -> None:
    _write_skill(tmp_path, "weekly-digest", "Weekly digest skill")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "buy groceries"})
    framed = await FrameStage().frame(request=request, config=FrameConfig(skill_loader=loader))
    assert framed.skill_match is None


@pytest.mark.asyncio
async def test_artifact_type_hint_from_skill_description(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "pr-reviewer",
        "Review pull request diffs and suggest improvements",
    )
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "Please review my pull request"})
    framed = await FrameStage().frame(request=request, config=FrameConfig(skill_loader=loader))
    assert framed.skill_match == "pr-reviewer"
    assert framed.artifact_type_hint == "pr"


@pytest.mark.asyncio
async def test_default_artifact_type_when_no_hint(tmp_path: Path) -> None:
    _write_skill(tmp_path, "digest", "A skill")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "ignore"})
    framed = await FrameStage().frame(
        request=request,
        config=FrameConfig(skill_loader=loader, default_artifact_type="direct_output"),
    )
    assert framed.artifact_type_hint == "direct_output"


@pytest.mark.asyncio
async def test_extracts_text_from_multiple_payload_keys(tmp_path: Path) -> None:
    _write_skill(tmp_path, "summarizer", "Summarize meeting notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"title": "Weekly meeting", "body": "Need a summary"})
    framed = await FrameStage().frame(request=request, config=FrameConfig(skill_loader=loader))
    assert framed.skill_match == "summarizer"
