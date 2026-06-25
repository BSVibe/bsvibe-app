"""FrameStage — derive skill/artifact-type hints + path branch from a Request.

Workflow §1.2 ("Frame") + §6 #5. The frame stage is the 2nd stage of the
3+ε state machine (Workflow §1). It is the *first LLM call* — it interprets
the trigger payload's meaning and, against the workspace's skill registry,
decides:

* the refined natural-language intent,
* which skill (if any) should handle the request — matched by *description*,
  not just keyword overlap,
* a hint about the deliverable artifact_type,
* the path branch (``knowledge_only`` | ``agent_loop``; Workflow §1.2). B9a
  *records* the classification; B9b is the branch that acts on
  ``knowledge_only`` (answer from BSage, skip the loop).

The framing uses ONE cheap LLM call via the :class:`FrameLlm` seam (resolved
per-workspace through the gateway, like the settle extractor). When no LLM is
resolvable (executor-only / no active account / a transient failure / malformed
output) it FALLS BACK to the deterministic keyword heuristic — the original
Phase 1 behaviour. Framing must never raise: a frame hiccup degrades, it never
breaks intake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import structlog

from backend.extensions.skill.loader import SkillLoader
from backend.workflow.infrastructure.intake.db import RequestRow

logger = structlog.get_logger(__name__)


# The path branch the frame stage classifies (Workflow §1.2 "Frame path
# branch"). B9a records the classification; B9b is the branch that ACTS on
# ``knowledge_only`` (answer from BSage, skip the loop). The agent-loop path is
# the default and behaves exactly as today.
PathClassification = Literal["knowledge_only", "agent_loop"]

# Phase 1 — the multi-stage pipeline shape. ``single`` is one run end-to-end
# (today's behaviour). ``design_then_impl`` marks a build that runs a DESIGN
# stage first (produce a spec), then has the orchestrator chain an
# IMPLEMENTATION stage that consumes it (P1-L2). Recorded on the frame; the
# orchestrator chaining + routing act on it.
PipelineKind = Literal["single", "design_then_impl"]


@dataclass
class FramedRequest:
    """Output of the ``frame`` stage — Workflow §1 stage 2."""

    skill_match: str | None
    artifact_type_hint: str | None
    # B9a — the LLM's refined natural-language intent (``None`` on the keyword
    # fallback path, which has no LLM to refine with).
    framed_intent: str | None = None
    # B9a — the path branch (Workflow §1.2). ``agent_loop`` keeps today's
    # behaviour; ``knowledge_only`` is recorded for B9b to act on.
    path_classification: PathClassification = "agent_loop"
    # P1-L2 — whether this request should run as a design→impl pipeline.
    pipeline: PipelineKind = "single"


# Cap the skill catalog we send to the cheap LLM so a workspace with many skills
# cannot blow the (local-model) framing budget. Descriptions are the match
# signal (Workflow §6 #5) so each is clamped, not dropped.
_FRAME_MAX_SKILLS = 40
_FRAME_MAX_DESC_CHARS = 300


@runtime_checkable
class FrameLlm(Protocol):
    """The single cheap-LLM seam the frame stage depends on.

    One plain text completion (no tools): given a system + user prompt, return
    the model's response text (expected to be a JSON object the stage parses).
    Production resolves a per-workspace gateway adapter; tests inject a stub.
    """

    async def complete_text(self, *, system: str, user: str) -> str: ...


@dataclass(slots=True)
class FrameConfig:
    """Caller-provided per-request frame context."""

    skill_loader: SkillLoader
    # Default artifact-type when the framer can't guess from skill content.
    default_artifact_type: str | None = None
    # B9a — the cheap-LLM seam. ``None`` (executor-only / no account / legacy
    # caller) → the keyword heuristic runs, preserving Phase 1 behaviour.
    llm: FrameLlm | None = None


# Artifact-type hints that denote a concrete deliverable to PRODUCE (vs. a
# pure-answer ask). Used by the knowledge_only coherence guard in
# :func:`_framed_from_llm` — any of these forces the agent loop.
_WORK_ARTIFACT_TYPES = frozenset({"code", "page", "page_image", "pr"})

# P1-L2 — a code/PR build whose intent reads "construct something" gets a
# DESIGN stage before implementation. Conservative word set: a tiny tweak
# ("fix the typo", "rename x") stays a single run. The orchestrator chains the
# impl stage only when the frame marks ``design_then_impl``.
_BUILD_INTENT_WORDS = frozenset(
    {
        "build",
        "implement",
        "feature",
        "app",
        "application",
        "service",
        "system",
        "design",
        "refactor",
        "integrate",
        "endpoint",
        "api",
        "module",
        "component",
        "pipeline",
    }
)


# answer-first (#12): a Direct request that is a *question* with no concrete
# work artifact is answered directly (``knowledge_only``), never routed into the
# agent loop where it has nothing to build and stands down. Deterministic so it
# holds with OR without the frame LLM — the prod dogfood that died on
# "지금 프로젝트 상황 어때?" hit the no-LLM keyword fallback, which historically
# forced ``agent_loop``.
#
# Korean has no token spaces, so its interrogative cues are matched as
# substrings; English interrogatives are matched as the leading token.
_KO_QUESTION_CUES: tuple[str, ...] = (
    "어때",
    "어떄",
    "어떻게",
    "무엇",
    "뭐야",
    "뭔가",
    "뭐예",
    "뭐죠",
    "어디",
    "언제",
    "누가",
    "얼마",
    "까요",
    "나요",
    "가요",
    "ㄹ까",
    "을까",
    "할까",
    "될까",
    "인가",
    "는가",
    "은가",
    "ㅂ니까",
    "습니까",
)
_EN_INTERROGATIVES: frozenset[str] = frozenset(
    {
        "what",
        "whats",
        "how",
        "hows",
        "why",
        "when",
        "where",
        "who",
        "which",
        "whose",
        "whom",
        "is",
        "are",
        "am",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
    }
)
# Verbs that imply DOING work — used to keep a build request phrased as a
# question ("can you build X?") on the agent loop while a genuine question
# ("how does X work?") is answered. Deliberately VERBS only: nouns like
# "api"/"system"/"module" appear in legitimate questions, so the broad
# :data:`_BUILD_INTENT_WORDS` set is wrong for this purpose.
_EN_BUILD_VERBS: frozenset[str] = frozenset(
    {
        "build",
        "implement",
        "create",
        "add",
        "make",
        "write",
        "fix",
        "refactor",
        "rewrite",
        "wire",
        "integrate",
        "generate",
    }
)
_KO_BUILD_STEMS: tuple[str, ...] = (
    "만들",
    "구현",
    "추가",
    "수정",
    "고쳐",
    "고치",
    "작성",
    "리팩터",
    "리팩토",
    "빌드",
    "생성",
    "연동",
)


def _looks_like_question(text: str) -> bool:
    """Deterministic: does the text read as a question? (`?`, a Korean
    interrogative cue, or a leading English interrogative.)"""
    if not text:
        return False
    stripped = text.strip()
    if "?" in stripped or "？" in stripped:
        return True
    low = stripped.lower()
    if any(cue in low for cue in _KO_QUESTION_CUES):
        return True
    tokens = low.split()
    return bool(tokens) and tokens[0].strip(".,!:;()\"'") in _EN_INTERROGATIVES


def _has_build_verb(text: str) -> bool:
    """Does the text carry a build VERB (real work to produce)?"""
    low = text.lower()
    tokens = {tok.strip(".,!?:;()\"'") for tok in low.split()}
    if tokens & _EN_BUILD_VERBS:
        return True
    return any(stem in low for stem in _KO_BUILD_STEMS)


def _is_answer_first_question(text: str, artifact_hint: str | None) -> bool:
    """answer-first (#12): a question with no concrete WORK artifact and no
    build verb is answered directly (``knowledge_only``)."""
    if artifact_hint in _WORK_ARTIFACT_TYPES:
        return False
    if not _looks_like_question(text):
        return False
    return not _has_build_verb(text)


def _derive_pipeline(artifact_hint: str | None, intent: str | None) -> PipelineKind:
    """``design_then_impl`` for a code/PR build whose intent implies
    construction; ``single`` otherwise (the default for everything else)."""
    if artifact_hint not in ("code", "pr"):
        return "single"
    words = {w.strip(".,!?:;()") for w in (intent or "").lower().split()}
    return "design_then_impl" if words & _BUILD_INTENT_WORDS else "single"


_FRAME_SYSTEM_PROMPT = (
    "You are the framing stage of an autonomous engineering workflow. Interpret "
    "the founder's request and respond with ONE JSON object (no prose, no code "
    "fences) with these keys:\n"
    '  "framed_intent": a one-sentence restatement of what the founder wants,\n'
    '  "skill_match": the EXACT name of the single best-matching skill from the '
    "catalog below (match on the skill's description), or null if none fits,\n"
    '  "artifact_type_hint": the likely deliverable type '
    '("code" | "page" | "page_image" | "pr" | null),\n'
    '  "path_classification": "knowledge_only" if the request can be answered '
    'purely from existing knowledge with no work, otherwise "agent_loop",\n'
    '  "pipeline": "single" for a focused one-pass task, or "design_then_impl" '
    "for substantial / multi-part work that genuinely benefits from a separate "
    "design pass (producing a spec) before implementation. Judge by COMPLEXITY "
    "and SCOPE, not keywords: a tiny tweak or a small focused endpoint is "
    '"single"; a multi-component system or cross-cutting build is '
    '"design_then_impl",\n'
    '  "pipeline_reason": a one-line justification for the pipeline choice.\n'
    "Only choose a skill_match that appears verbatim in the catalog."
)


class FrameStage:
    """Convert a raw Request into a framed plan."""

    async def frame(self, *, request: RequestRow, config: FrameConfig) -> FramedRequest:
        """Inspect the request and return framing hints.

        Uses the cheap LLM when one is configured; degrades to the keyword
        heuristic on no-LLM / failure / malformed output. Never raises."""
        text = _extract_text(request)
        framed = await self._frame_via_llm(text=text, config=config)
        if framed is None:
            framed = _frame_via_keyword(text=text, config=config)
        logger.info(
            "frame_stage_resolved",
            request_id=str(request.id),
            workspace_id=str(request.workspace_id),
            skill_match=framed.skill_match,
            artifact_type_hint=framed.artifact_type_hint,
            path_classification=framed.path_classification,
            used_llm=config.llm is not None and framed.framed_intent is not None,
        )
        return framed

    async def _frame_via_llm(self, *, text: str, config: FrameConfig) -> FramedRequest | None:
        """Run the single cheap-LLM framing call, or ``None`` to fall back.

        Returns ``None`` (caller falls back to the keyword heuristic) when there
        is no LLM, the call fails, or the output cannot be parsed — framing must
        never raise."""
        llm = config.llm
        if llm is None:
            return None
        user_prompt = _build_user_prompt(text, config.skill_loader)
        try:
            raw = await llm.complete_text(system=_FRAME_SYSTEM_PROMPT, user=user_prompt)
        except Exception:  # noqa: BLE001 — framing must never break intake
            logger.warning("frame_stage_llm_failed", exc_info=True)
            return None
        parsed = _parse_frame_json(raw)
        if parsed is None:
            logger.warning("frame_stage_llm_unparseable")
            return None
        return _framed_from_llm(parsed, config, text)


# --------------------------------------------------------------------------
# LLM framing helpers
# --------------------------------------------------------------------------


def _build_user_prompt(text: str, loader: SkillLoader) -> str:
    """Compose the user prompt: the request text + the workspace skill catalog."""
    lines = [f"Request:\n{text or '(empty request)'}", "", "Skill catalog:"]
    skills = list(loader.registry.values())[:_FRAME_MAX_SKILLS]
    if not skills:
        lines.append("(no skills installed)")
    else:
        for skill in skills:
            desc = skill.description[:_FRAME_MAX_DESC_CHARS]
            lines.append(f"- {skill.name}: {desc}")
    return "\n".join(lines)


def _parse_frame_json(raw: str) -> dict[str, Any] | None:
    """Parse the LLM's JSON framing, tolerating a leading/trailing code fence."""
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    # Tolerate a ```json fenced block — strip to the first/last brace.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(candidate[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _framed_from_llm(parsed: dict[str, Any], config: FrameConfig, text: str) -> FramedRequest:
    """Build a :class:`FramedRequest` from the parsed LLM JSON, validated.

    A hallucinated ``skill_match`` (not in the loader's registry) is dropped —
    we never trust the LLM to name a skill that doesn't exist."""
    loader = config.skill_loader
    skill_match = parsed.get("skill_match")
    if not isinstance(skill_match, str) or skill_match not in loader.registry:
        skill_match = None

    artifact_hint = parsed.get("artifact_type_hint")
    if not isinstance(artifact_hint, str) or not artifact_hint:
        artifact_hint = None
    if artifact_hint is None:
        artifact_hint = config.default_artifact_type

    framed_intent = parsed.get("framed_intent")
    if not isinstance(framed_intent, str) or not framed_intent.strip():
        framed_intent = None

    path = parsed.get("path_classification")
    path_classification: PathClassification = "agent_loop"
    # Coherence guard: a concrete WORK artifact (code/page/page_image/pr) means
    # there is something to PRODUCE, which contradicts ``knowledge_only``
    # ("answerable from existing knowledge with no work"). Local models are
    # unreliable on this binary and emit the incoherent pair — trusting it routes
    # real work to a text-only answer that ships nothing (prod dogfood
    # 2026-05-28: "Create a Python file calc.py" → knowledge_answer, no file,
    # yet shipped). A concrete artifact always wins; only an artifact-less ask
    # (None / direct_output) may stay knowledge_only.
    if path == "knowledge_only" and artifact_hint not in _WORK_ARTIFACT_TYPES:
        path_classification = "knowledge_only"

    # answer-first (#12): a genuine question with no concrete work artifact is
    # answered directly even when the LLM routed it to the loop — the founder
    # rule is "questions are answered first". A build request phrased as a
    # question keeps a work ``artifact_hint`` (or a build verb) and stays on the
    # loop. This mirrors the keyword-fallback behaviour so both paths agree.
    if path_classification == "agent_loop" and _is_answer_first_question(text, artifact_hint):
        path_classification = "knowledge_only"

    pipeline = _resolve_pipeline(parsed, artifact_hint, framed_intent)

    return FramedRequest(
        skill_match=skill_match,
        artifact_type_hint=artifact_hint,
        framed_intent=framed_intent,
        path_classification=path_classification,
        pipeline=pipeline,
    )


# Valid ``PipelineKind`` values, used to validate the LLM's ``pipeline`` field.
# We never trust a hallucinated value: only a verbatim-valid kind is honoured.
_VALID_PIPELINES: frozenset[str] = frozenset({"single", "design_then_impl"})


def _resolve_pipeline(
    parsed: dict[str, Any], artifact_hint: str | None, framed_intent: str | None
) -> PipelineKind:
    """Decide the pipeline kind from the LLM output, with defensive guards.

    Precedence (P1-L2 / D1 — the complexity judgment is the LLM's, not a
    keyword rule):

    1. Honour the LLM's ``pipeline`` ONLY when it is a verbatim-valid
       :data:`PipelineKind`. An absent / missing / hallucinated value is never
       trusted — we fall back to :func:`_derive_pipeline` (the keyword rule, the
       no-LLM behaviour), so a malformed frame degrades, never breaks.
    2. Coherence guard (mirrors the ``path_classification`` guard above): a
       ``single`` / ``design_then_impl`` distinction only makes sense for a
       concrete WORK artifact (something to PRODUCE). A pure-answer ask
       (artifact_hint not in :data:`_WORK_ARTIFACT_TYPES`) is always ``single``,
       regardless of what the LLM emits — there is no implementation to stage.
    """
    if artifact_hint not in _WORK_ARTIFACT_TYPES:
        return "single"
    raw = parsed.get("pipeline")
    if isinstance(raw, str) and raw in _VALID_PIPELINES:
        # mypy: ``raw in _VALID_PIPELINES`` narrows to the Literal at runtime,
        # but the type checker can't see it — cast via the known-good branch.
        return "design_then_impl" if raw == "design_then_impl" else "single"
    logger.info("frame_stage_pipeline_keyword_fallback", raw_pipeline=raw)
    return _derive_pipeline(artifact_hint, framed_intent)


# --------------------------------------------------------------------------
# Keyword heuristic fallback (original Phase 1 behaviour)
# --------------------------------------------------------------------------


def _frame_via_keyword(*, text: str, config: FrameConfig) -> FramedRequest:
    """The deterministic keyword heuristic — Phase 1 behaviour, no LLM.

    Classifies the path as ``agent_loop`` (the loop drives, as today) EXCEPT for
    a genuine question with no work artifact, which is answered first (#12,
    ``knowledge_only``) — so a Direct question never silently routes into a
    coding loop just because no frame LLM was resolvable."""
    skill_match = _match_skill(text, config.skill_loader)
    artifact_hint = _guess_artifact_type(skill_match, config.skill_loader) or (
        config.default_artifact_type
    )
    path: PathClassification = (
        "knowledge_only" if _is_answer_first_question(text, artifact_hint) else "agent_loop"
    )
    return FramedRequest(
        skill_match=skill_match,
        artifact_type_hint=artifact_hint,
        framed_intent=None,
        path_classification=path,
        pipeline=_derive_pipeline(artifact_hint, text),
    )


def _extract_text(request: RequestRow) -> str:
    """Pull a flat text representation out of a Request payload."""
    payload = request.payload or {}
    parts: list[str] = []
    if isinstance(payload, dict):
        for key in ("text", "title", "summary", "body", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).lower()


def _match_skill(text: str, loader: SkillLoader) -> str | None:
    """Pick the first skill whose name or description is referenced in the text."""
    if not text:
        return None
    for skill in loader.registry.values():
        haystack = f"{skill.name} {skill.description}".lower()
        # Crude but deterministic: any tokenized keyword from the skill
        # description appearing in the request text counts as a match.
        for word in haystack.split():
            if len(word) >= 4 and word in text:
                return skill.name
    return None


_ARTIFACT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("pull request", "pr"),
    ("page", "page"),
    ("image", "page_image"),
    ("code", "code"),
)


def _guess_artifact_type(skill_name: str | None, loader: SkillLoader) -> str | None:
    """If the skill's description hints at an artifact_type, surface it."""
    if skill_name is None:
        return None
    skill = loader.registry.get(skill_name)
    if skill is None:
        return None
    desc = skill.description.lower()
    for keyword, artifact in _ARTIFACT_KEYWORDS:
        if keyword in desc:
            return artifact
    return None


__all__ = [
    "FrameConfig",
    "FrameLlm",
    "FrameStage",
    "FramedRequest",
    "PathClassification",
    "PipelineKind",
]
