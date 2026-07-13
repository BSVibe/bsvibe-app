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
per-workspace through the gateway, like the settle extractor). The path branch is
the LLM's judgment — ASK vs PRODUCE (:data:`_PATH_RUBRIC`) — and is never guessed
from keywords: the heuristic this replaced (interrogative cues + build verbs,
Korean and English only) read grammar rather than intent, so an ask phrased as an
imperative ("현 프로젝트 상황 설명해줘") was handed to a coding executor, which had
nothing to build and shipped an unrelated diff (prod run ff1615e8, 2026-07-13).
When no verdict is obtainable the stage raises :class:`FrameUnclassifiedError` —
the caller fails the run explicitly rather than silently picking a kind.
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
    # B9a — the LLM's refined natural-language intent.
    framed_intent: str | None = None
    # L8 — a SHORT, plain-language title of the task (≤ ~8 words, no file paths /
    # type signatures / code identifiers) the founder-facing review surfaces lead
    # with instead of the raw, developer-y Direction. ``None`` when the model
    # omits it — the surface then falls back to framed_intent / intent.
    summary_title: str | None = None
    # B9a — the path branch (Workflow §1.2), always the LLM's verdict.
    path_classification: PathClassification = "agent_loop"
    # P1-L2 — whether this request should run as a design→impl pipeline.
    pipeline: PipelineKind = "single"


# Cap the skill catalog we send to the cheap LLM so a workspace with many skills
# cannot blow the (local-model) framing budget. Descriptions are the match
# signal (Workflow §6 #5) so each is clamped, not dropped.
# L8 — hard cap on the plain-language ``summary_title`` so a verbose model can't
# blow up a review row (the prompt asks for ≤ 8 words; this is the backstop).
_SUMMARY_TITLE_CAP = 120
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
    # B9a — the cheap-LLM seam. ``None`` (no frame route resolved) is not a
    # fallback: the stage raises :class:`FrameUnclassifiedError`.
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


def _derive_pipeline(artifact_hint: str | None, intent: str | None) -> PipelineKind:
    """``design_then_impl`` for a code/PR build whose intent implies
    construction; ``single`` otherwise (the default for everything else)."""
    if artifact_hint not in ("code", "pr"):
        return "single"
    words = {w.strip(".,!?:;()") for w in (intent or "").lower().split()}
    return "design_then_impl" if words & _BUILD_INTENT_WORDS else "single"


#: The ASK-vs-PRODUCE rubric — the single definition of what makes a request a
#: question. It is stated for the LLM because it is a semantic judgment, not a
#: lexical one: the deleted keyword heuristic (interrogative cues + build verbs,
#: Korean and English only) could not see that "설명해줘" / "explain X" ask by
#: telling, and it had nothing at all to say about any other language.
ASK_VS_PRODUCE_RUBRIC = (
    "Decide by what the founder wants BACK — never by grammar, mood, punctuation, "
    "or language:\n"
    "- ASK: they want to be TOLD something — the project's status, an explanation, "
    "a summary, a history, an opinion. The reply itself IS the deliverable. This "
    'holds when the ask is phrased as a command ("explain the routing", '
    '"상황 설명해줘", "状況を教えて"), and it holds even when answering requires '
    "consulting the workspace's knowledge or the product's recorded state. If "
    "nothing would be created or changed, it is an ASK.\n"
    "- PRODUCE: they want something MADE or CHANGED — code, a page, a PR, a "
    "document, a config edit. Some artifact must exist or differ afterwards.\n"
    'When a request both produces and explains ("build X and tell me how it works"), '
    "producing wins: PRODUCE."
)

_PATH_RUBRIC = (
    '  "path_classification": "knowledge_only" (an ASK) or "agent_loop" (a PRODUCE). '
    + ASK_VS_PRODUCE_RUBRIC.replace("\n", "\n    ")
    + "\n"
)

_FRAME_SYSTEM_PROMPT = (
    "You are the framing stage of an autonomous engineering workflow. Interpret "
    "the founder's request and respond with ONE JSON object (no prose, no code "
    "fences) with these keys:\n"
    '  "framed_intent": a one-sentence restatement of what the founder wants,\n'
    '  "summary_title": a SHORT (max 8 words) plain-language title of the task '
    "that a non-developer can scan — NO file paths, NO type signatures, NO code "
    'identifiers in backticks, and NO "in the X product" preamble. Write it in '
    "the SAME language as framed_intent (e.g. 'Add a mean helper', not 'add "
    "`mean(values: list[float]) -> float` in backend/common/mean.py'),\n"
    '  "skill_match": the EXACT name of the single best-matching skill from the '
    "catalog below (match on the skill's description), or null if none fits,\n"
    '  "artifact_type_hint": the likely deliverable type '
    '("code" | "page" | "page_image" | "pr" | null). A pure answer has null,\n'
    + _PATH_RUBRIC
    + '  "pipeline": "single" for a focused one-pass task, or "design_then_impl" '
    "for substantial / multi-part work that genuinely benefits from a separate "
    "design pass (producing a spec) before implementation. Judge by COMPLEXITY "
    "and SCOPE, not keywords: a tiny tweak or a small focused endpoint is "
    '"single"; a multi-component system or cross-cutting build is '
    '"design_then_impl",\n'
    '  "pipeline_reason": a one-line justification for the pipeline choice.\n'
    "Only choose a skill_match that appears verbatim in the catalog."
)


class FrameUnclassifiedError(RuntimeError):
    """The frame LLM produced no usable ``path_classification``.

    Raised when the call fails, the output is unparseable, or the verdict is
    missing/invalid. The stage must NOT guess: guessing ``agent_loop`` hands a
    question to a coding executor, which has nothing to build and edits whatever
    it finds (prod run ff1615e8 — "현 프로젝트 상황 설명해줘" shipped an unrelated
    diff). Guessing ``knowledge_only`` silently answers instead of building. Per
    no-implicit-routing, an undecidable route is an explicit error — the caller
    fails the run and the founder sees why.
    """


class FrameModelUnresolvedError(FrameUnclassifiedError):
    """No frame model is routed for this workspace, so nothing can classify.

    Distinct from its parent because the remedy is different: this is the
    familiar "no model account" condition, and the caller pauses the run on a
    Decision (founder picks a model) rather than failing it — the same UX an
    unresolved act-stage account already gets.
    """


class FrameStage:
    """Convert a raw Request into a framed plan."""

    async def frame(self, *, request: RequestRow, config: FrameConfig) -> FramedRequest:
        """Inspect the request and return framing hints.

        Raises :class:`FrameUnclassifiedError` when the frame LLM yields no
        verdict — the run's KIND (answer vs. build) is never guessed."""
        text = _extract_text(request)
        framed = await self._frame_via_llm(text=text, config=config)
        logger.info(
            "frame_stage_resolved",
            request_id=str(request.id),
            workspace_id=str(request.workspace_id),
            skill_match=framed.skill_match,
            artifact_type_hint=framed.artifact_type_hint,
            path_classification=framed.path_classification,
        )
        return framed

    async def _frame_via_llm(self, *, text: str, config: FrameConfig) -> FramedRequest:
        """Run the single cheap-LLM framing call."""
        llm = config.llm
        if llm is None:
            raise FrameModelUnresolvedError("no frame model is routed for this workspace")
        user_prompt = _build_user_prompt(text, config.skill_loader)
        try:
            raw = await llm.complete_text(system=_FRAME_SYSTEM_PROMPT, user=user_prompt)
        except Exception as exc:
            logger.warning("frame_stage_llm_failed", exc_info=True)
            raise FrameUnclassifiedError("the frame model call failed") from exc
        parsed = _parse_frame_json(raw)
        if parsed is None:
            logger.warning("frame_stage_llm_unparseable")
            raise FrameUnclassifiedError("the frame model returned unparseable output")
        return _framed_from_llm(parsed, config)


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


def _framed_from_llm(parsed: dict[str, Any], config: FrameConfig) -> FramedRequest:
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

    # L8 — short plain-language title for the review surfaces. Trimmed + length-
    # capped (a verbose model can't blow the row); blank / non-string → None so
    # the surface falls back to framed_intent / intent.
    summary_title = parsed.get("summary_title")
    if not isinstance(summary_title, str) or not summary_title.strip():
        summary_title = None
    else:
        summary_title = summary_title.strip()[:_SUMMARY_TITLE_CAP]

    # The run's KIND is the LLM's verdict (rubric: :data:`_PATH_RUBRIC`) and only
    # the LLM's — a missing or invalid value is NOT defaulted, because both
    # possible defaults are destructive (see :class:`FrameUnclassifiedError`).
    path = parsed.get("path_classification")
    if path not in ("knowledge_only", "agent_loop"):
        raise FrameUnclassifiedError(f"the frame model returned no valid verdict: {path!r}")
    path_classification: PathClassification = path

    # Coherence guard: a concrete WORK artifact (code/page/page_image/pr) means
    # there is something to PRODUCE, which contradicts ``knowledge_only``. Local
    # models are unreliable on this binary and emit the incoherent pair —
    # trusting it routes real work to a text-only answer that ships nothing (prod
    # dogfood 2026-05-28: "Create a Python file calc.py" → knowledge_answer, no
    # file, yet shipped). A concrete artifact always wins; only an artifact-less
    # ask (None / direct_output) may stay knowledge_only. Note the guard is
    # one-directional on purpose: ``direct_output`` is what an answer AND a prose
    # deliverable (a blog post, a report) both carry, so it cannot pull the other
    # way — that judgment is the rubric's.
    if path_classification == "knowledge_only" and artifact_hint in _WORK_ARTIFACT_TYPES:
        path_classification = "agent_loop"

    pipeline = _resolve_pipeline(parsed, artifact_hint, framed_intent)

    return FramedRequest(
        skill_match=skill_match,
        artifact_type_hint=artifact_hint,
        framed_intent=framed_intent,
        summary_title=summary_title,
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


def _extract_text(request: RequestRow) -> str:
    """Pull a flat text representation out of a Request payload."""
    payload = request.payload or {}
    parts: list[str] = []
    if isinstance(payload, dict):
        # ``intent_text`` is the canonical directive field a connector-sourced
        # request carries (github issue / PR / comment via the webhook parser);
        # it must be read here, consistent with ``_request_intent_text``, or the
        # frame sees "no task" and degrades a real build to a knowledge answer.
        for key in ("intent_text", "text", "title", "summary", "body", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).lower()


__all__ = [
    "FrameConfig",
    "FrameLlm",
    "FrameModelUnresolvedError",
    "FrameStage",
    "FrameUnclassifiedError",
    "FramedRequest",
    "PathClassification",
    "PipelineKind",
]
