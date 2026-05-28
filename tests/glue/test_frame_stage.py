"""FrameStage — keyword skill match + artifact_type hint, plus real LLM framing."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.intake.db import RequestRow, RequestStatus
from backend.orchestrator.frame import FrameConfig, FrameLlm, FrameStage
from backend.skills.loader import SkillLoader


class _StubFrameLlm:
    """Deterministic :class:`FrameLlm` — returns a canned JSON framing.

    Records every prompt it saw so tests can assert the catalog was passed.
    """

    def __init__(self, response: str | dict[str, Any]) -> None:
        self._response = response if isinstance(response, str) else json.dumps(response)
        self.calls: list[str] = []

    async def complete_text(self, *, system: str, user: str) -> str:
        self.calls.append(user)
        return self._response


class _RaisingFrameLlm:
    """A :class:`FrameLlm` whose call always raises — exercises graceful fallback."""

    async def complete_text(self, *, system: str, user: str) -> str:
        raise RuntimeError("gateway exploded")


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


# --------------------------------------------------------------------------
# B9a — real cheap-LLM framing (graceful fallback to keyword heuristic)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_framing_picks_skill_by_description(tmp_path: Path) -> None:
    """With a FrameLlm seam, the framer uses the LLM's structured framing —
    skill match by description, artifact-type, refined intent, path class."""
    _write_skill(tmp_path, "prd-writer", "Draft a product requirements document")
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    llm = _StubFrameLlm(
        {
            "framed_intent": "Write a PRD for the new onboarding flow",
            "skill_match": "prd-writer",
            "artifact_type_hint": "page",
            "path_classification": "agent_loop",
        }
    )
    request = _request({"text": "i need a spec doc for onboarding"})
    framed = await FrameStage().frame(
        request=request, config=FrameConfig(skill_loader=loader, llm=llm)
    )
    assert framed.skill_match == "prd-writer"
    assert framed.artifact_type_hint == "page"
    assert framed.framed_intent == "Write a PRD for the new onboarding flow"
    assert framed.path_classification == "agent_loop"
    # The LLM was given the workspace's skill catalog (by description) to match on.
    assert "prd-writer" in llm.calls[0]
    assert "Draft a product requirements document" in llm.calls[0]


@pytest.mark.asyncio
async def test_llm_framing_classifies_knowledge_only_path(tmp_path: Path) -> None:
    """Frame records a ``knowledge_only`` path classification (B9b acts on it)."""
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    llm = _StubFrameLlm(
        {
            "framed_intent": "What is our deployment process?",
            "skill_match": None,
            "artifact_type_hint": None,
            "path_classification": "knowledge_only",
        }
    )
    request = _request({"text": "what is our deploy process?"})
    framed = await FrameStage().frame(
        request=request, config=FrameConfig(skill_loader=loader, llm=llm)
    )
    assert framed.path_classification == "knowledge_only"
    assert framed.skill_match is None


@pytest.mark.asyncio
async def test_llm_knowledge_only_with_concrete_artifact_coerced_to_agent_loop(
    tmp_path: Path,
) -> None:
    """Coherence guard: a concrete ``artifact_type_hint`` (code/page/pr/...)
    contradicts ``knowledge_only`` ("answerable with no work"). Producing an
    artifact IS work, so the frame must coerce the path to ``agent_loop``.

    Dogfood (2026-05-28, prod): the local model classified "Create a Python
    file calc.py with a function multiply" as ``knowledge_only`` while also
    hinting ``artifact_type_hint="code"``. B9b then routed it to the
    KnowledgeAnswerOrchestrator, which answered with code-in-text and wrote
    NO file, yet the run was marked shipped. We never trust an incoherent
    classification — a concrete artifact wins over the knowledge_only flag."""
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    llm = _StubFrameLlm(
        {
            "framed_intent": "Create a Python file calc.py with multiply(a, b).",
            "skill_match": None,
            "artifact_type_hint": "code",
            "path_classification": "knowledge_only",
        }
    )
    request = _request({"text": "Create a Python file calc.py with multiply(a, b)."})
    framed = await FrameStage().frame(
        request=request, config=FrameConfig(skill_loader=loader, llm=llm)
    )
    assert framed.artifact_type_hint == "code"
    assert framed.path_classification == "agent_loop"


@pytest.mark.asyncio
async def test_llm_framing_rejects_hallucinated_skill(tmp_path: Path) -> None:
    """An LLM that names a skill not in the catalog is ignored (no false match)."""
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    llm = _StubFrameLlm(
        {
            "framed_intent": "Do the thing",
            "skill_match": "totally-made-up-skill",
            "artifact_type_hint": "code",
            "path_classification": "agent_loop",
        }
    )
    request = _request({"text": "do the thing"})
    framed = await FrameStage().frame(
        request=request, config=FrameConfig(skill_loader=loader, llm=llm)
    )
    assert framed.skill_match is None


@pytest.mark.asyncio
async def test_falls_back_to_keyword_when_no_llm(tmp_path: Path) -> None:
    """No FrameLlm seam → the keyword heuristic runs (existing behaviour)."""
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from recent notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "Please create the weekly digest for this week"})
    framed = await FrameStage().frame(request=request, config=FrameConfig(skill_loader=loader))
    assert framed.skill_match == "weekly-digest"
    # No-LLM path classifies as agent_loop (the loop drives, as today).
    assert framed.path_classification == "agent_loop"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_keyword(tmp_path: Path) -> None:
    """A FrameLlm that raises must NOT break intake — fall back to keywords."""
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from recent notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "Please create the weekly digest for this week"})
    framed = await FrameStage().frame(
        request=request,
        config=FrameConfig(skill_loader=loader, llm=_RaisingFrameLlm()),
    )
    assert framed.skill_match == "weekly-digest"
    assert framed.path_classification == "agent_loop"


@pytest.mark.asyncio
async def test_llm_malformed_json_falls_back_to_keyword(tmp_path: Path) -> None:
    """A FrameLlm returning non-JSON garbage falls back to the keyword heuristic."""
    _write_skill(tmp_path, "weekly-digest", "Generate a weekly digest from recent notes")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    request = _request({"text": "Please create the weekly digest for this week"})
    framed = await FrameStage().frame(
        request=request,
        config=FrameConfig(skill_loader=loader, llm=_StubFrameLlm("not json at all")),
    )
    assert framed.skill_match == "weekly-digest"


def test_frame_llm_protocol_is_runtime_checkable() -> None:
    assert isinstance(_StubFrameLlm({"path_classification": "agent_loop"}), FrameLlm)
