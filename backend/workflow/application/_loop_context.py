"""Loop-context helpers — prompts + initial-message assembly.

Lifted from ``backend.execution.orchestrator`` (Lift H2a sub-split / v8
§17.1). Owns the *static* loop ingredients (system prompt, design-spec
directive) and the *pre-cycle context assembly* (knowledge seed, design
seed, design directive, suggested-skill hint, resumption messages,
``knowledge_search`` handler, ``invoke_skill`` adapter).

This module is private to the agent-loop conductor — kept under the
``application/`` layer so it shares the loop's coordinate system but
isolated from the conductor file so neither breaches the 600 LOC ceiling.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.config import Settings
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.tool_binding import INVOKE_SKILL_NAME, register_invoke_skill
from backend.workflow.application.tool_registry import (
    _KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT,
    _KNOWLEDGE_SEED_MAX_RESULTS,
    make_knowledge_search_handler,
)
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.tools import ToolRegistry

# ``make_knowledge_search_handler`` is imported (and re-exported via ``__all__``) from the clean
# ``tool_registry`` module so the worker's existing callers keep importing it from here while the
# MCP transport — forbidden from importing this ``backend.extensions``-tainted module — registers
# the same handler through the shared factory.

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are an autonomous engineer working inside a sandboxed workspace. "
    "Use the tools to inspect and change files. You MUST call "
    "declare_verification BEFORE any file_write or file_edit — those tools are "
    "REFUSED until you do — to commit to how the work will be checked (prefer a "
    "command check that runs the real test/lint, scoped to the files you "
    "changed). Reading files (file_read, file_list) is allowed first. When the "
    "step is complete, stop calling tools and reply with a short plain-text "
    "summary — that triggers verification. If you are blocked on a decision "
    "only the founder can make, call ask_user_question. "
    "W2 — your work is committed to a per-run git branch and merged into the "
    "product's main on verify. If verify reports a merge conflict, the "
    "conflicting files in your workspace will contain '<<<<<<<', '=======', "
    "and '>>>>>>>' markers. Resolve them with file_read/file_edit (you can "
    "also `shell_exec git log/diff/show` to inspect main's intent) and "
    "re-trigger verification by re-replying. If the conflict is semantically "
    "ambiguous — i.e. you can't tell which intent to honor — call "
    "ask_user_question with a clear semantic question (e.g., 'main added X "
    "while this branch added Y at the same spot — should X replace Y, or "
    "should both coexist?'). Never paste raw conflict markers to the founder."
)

# D1b — when a run is the DESIGN stage of a ``design_then_impl`` pipeline, it
# must produce a SPECIFICATION (a concise markdown spec the impl stage
# implements), NOT finished code. Before D1b the design run got only the generic
# work prompt, so it built working code the impl stage regenerated — a no-op
# merge (2026-05-28 dogfood). This directive, seeded into the loop's initial
# context for a design-stage run, redirects it to spec. One concise instruction
# block (respect the local-model generation budget). The ``single`` + ``impl``
# runs never get it (impl IMPLEMENTS the spec). Kept byte-identical to the
# executor path's directive so both prompt-assembly sites tell the design run
# the same thing.
_DESIGN_SPEC_DIRECTIVE = (
    "THIS IS THE DESIGN STAGE. Write ONE concise markdown specification — do NOT "
    "implement it and do NOT write working code; a later implementation stage "
    "will. The spec MUST cover: Goal (what to build and why), "
    "Interface/Contract (the public API, signatures, inputs/outputs), File "
    "layout (the files to create and what each holds), and Acceptance criteria "
    "(observable conditions that prove the implementation is correct). Keep it "
    "tight and implementable; output only the spec."
)


def _is_design_stage(run: ExecutionRun) -> bool:
    """D1b — True when this run is the DESIGN stage of a ``design_then_impl``
    pipeline (so the loop is told to spec, not build).

    Mirrors routing's ``_derive_stage`` + the executor path's ``_is_design_stage``:
    the FIRST run of a ``design_then_impl`` pipeline carries no explicit
    ``stage`` (the AgentRunner chains impl off the frame's pipeline signal), so
    an unset / non-``impl`` stage on such a run IS the design stage. The spawned
    impl run (``stage="impl"``) is excluded — it implements the spec. Any other
    pipeline (``single`` / no frame) is excluded. Tolerant of an odd payload."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    raw_frame = payload.get("frame")
    frame = raw_frame if isinstance(raw_frame, dict) else {}
    if frame.get("pipeline") != "design_then_impl":
        return False
    return payload.get("stage") != "impl"


def _intent_title(run: ExecutionRun) -> str:
    payload = run.payload or {}
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:512]


def _resumption_messages(run: ExecutionRun) -> list[dict[str, Any]]:
    """Build loop seed messages for any founder-resolved decisions.

    ``run.payload["resolved_decisions"]`` is a list of
    ``{decision_id, question, answer}`` appended by the checkpoints resolve
    endpoint. Each becomes a user message so the work LLM continues with the
    founder's answer in context instead of re-asking the blocking question."""
    payload = run.payload or {}
    resolved = payload.get("resolved_decisions") if isinstance(payload, dict) else None
    if not isinstance(resolved, list):
        return []
    messages: list[dict[str, Any]] = []
    for entry in resolved:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if not answer:
            continue
        messages.append(
            {
                "role": "user",
                "content": (
                    "The founder resolved a prior question — "
                    f"Q: {question} A: {answer}. "
                    "Continue the work with this decision."
                ),
            }
        )
    return messages


class _RetrieverSearcher:
    """Adapt a CanonRetriever to the skill runner's ``Searcher``.

    The skill runner primes a skill's system prompt via ``search(query, *,
    top_k, max_chars) -> str``; the retriever speaks ``retrieve_for_signals
    (signals) -> list[str]``. This thin adapter joins the canonical statements
    into the formatted-string shape the runner expects, capped at ``max_chars``,
    and degrades to an empty string when there is no knowledge (never raises —
    matching the retriever's own graceful-empty contract)."""

    def __init__(self, retriever: Any) -> None:
        self._retriever = retriever

    async def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        max_chars: int = 50_000,
    ) -> str:
        try:
            statements = await self._retriever.retrieve_for_signals(query)
        except Exception:  # noqa: BLE001 — priming must never crash a skill run
            logger.warning("skill_searcher_retrieve_failed", exc_info=True)
            return ""
        cleaned = [s.strip() for s in statements if s and s.strip()][:top_k]
        if not cleaned:
            return ""
        return "\n".join(f"- {s}" for s in cleaned)[:max_chars]


async def knowledge_seed_message(run: ExecutionRun, *, retriever: Any) -> dict[str, Any] | None:
    """B6 — build the loop-start knowledge seed for ``run``, or ``None``.

    Retrieves canon relevant to the run's STABLE intent (the same text the
    first user turn uses — never written_paths, none exist yet) and folds the
    top statements into a single context message so the work is informed by
    the workspace's established patterns BEFORE the act/verify cycle. No
    retriever / no patterns → ``None`` (inject nothing; an empty-knowledge
    workspace stays byte-identical to pre-B6). Never raises — a retrieval
    hiccup degrades to no seed, exactly like the B3 verify fold."""
    if retriever is None:
        return None
    signals = _intent_title(run)
    try:
        statements = await retriever.retrieve_for_signals(signals)
    except Exception:  # noqa: BLE001 — seeding must never crash the loop
        logger.warning("knowledge_seed_retrieve_failed", run_id=str(run.id), exc_info=True)
        return None
    cleaned = [
        s.strip()[:_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT] for s in statements if s and s.strip()
    ][:_KNOWLEDGE_SEED_MAX_RESULTS]
    if not cleaned:
        return None
    body = "\n".join(f"- {s}" for s in cleaned)
    logger.info("knowledge_seeded", run_id=str(run.id), count=len(cleaned))
    return {
        "role": "system",
        "content": (
            "Relevant established patterns for this workspace (consider them as you work):\n" + body
        ),
    }


def design_directive_message(run: ExecutionRun) -> dict[str, Any] | None:
    """D1b — when this run is the DESIGN stage of a ``design_then_impl``
    pipeline, seed the spec-only directive so the loop writes a spec rather
    than finished code (the impl stage implements it).

    ``None`` for a single run, an impl-stage run, or a run with no frame
    (loop unchanged)."""
    if not _is_design_stage(run):
        return None
    logger.info("design_directive_seeded", run_id=str(run.id))
    return {"role": "system", "content": _DESIGN_SPEC_DIRECTIVE}


def design_seed_message(run: ExecutionRun, *, settings: Settings) -> dict[str, Any] | None:
    """P1-L2b — fold the prior design stage's spec into the loop-start
    context when this run is the impl stage of a design→impl handoff.

    ``None`` for a non-impl run (no design refs) or when no spec content is
    readable — best-effort, never raises into the loop."""
    from backend.workflow.application.handoff import read_design_context  # noqa: PLC0415

    content = read_design_context(run, settings)
    if content is None:
        return None
    logger.info("design_seeded", run_id=str(run.id))
    return {"role": "system", "content": content}


def suggested_skill_message(
    *, suggested_skill: str | None, suggested_skill_description: str | None
) -> dict[str, Any] | None:
    """B9a — the frame-matched skill hint for the loop's initial context.

    ``None`` when the frame matched no skill (the hint is omitted, loop
    unchanged). The message names the skill + its description and points the
    work LLM at ``invoke_skill`` — a hint, not a forced first action."""
    if not suggested_skill:
        return None
    description = suggested_skill_description or ""
    suffix = f" — {description}" if description else ""
    return {
        "role": "system",
        "content": (
            f"Suggested skill for this task: {suggested_skill}{suffix}. "
            f"Invoke it via invoke_skill if appropriate for the work."
        ),
    }


def register_invoke_skill_tool(
    registry: ToolRegistry,
    *,
    skill_loader: SkillLoader | None,
    retriever: Any,
    completion_fn: Any,
) -> list[str]:
    """Register ``invoke_skill`` into ``registry`` — the WORKER-ONLY tool.

    Separate from ``knowledge_search`` (now built by the shared
    :func:`~backend.workflow.application.tool_registry.assemble_run_tool_registry`) because
    ``register_invoke_skill`` lives in ``backend.extensions.skill`` — a context the MCP transport
    is forbidden to import. So the in-process loop adds it here, after the shared factory; the MCP
    path leaves it out until a follow-up threads a ``SkillLoader`` + completion fn in from the
    composition root (INV-7 #1, follow-up).

    Only when a workspace :class:`SkillLoader` is provided (the production worker factory always
    threads one in). Returns the names added so the caller can fold them into the surfaced tool
    schema. A missing loader → empty list."""
    if skill_loader is None:
        return []
    searcher = _RetrieverSearcher(retriever) if retriever is not None else None
    # invoke_skill — runs a named workspace skill end-to-end. The skill runner's completion seam
    # routes through the SAME loop LLM (adapted to its (system_prompt, user_input) shape); the
    # optional searcher primes the skill's system prompt with retrieved knowledge.
    register_invoke_skill(
        registry,
        loader=skill_loader,
        completion_fn=completion_fn,
        searcher=searcher,
    )
    return [INVOKE_SKILL_NAME]


__all__ = [
    "_DESIGN_SPEC_DIRECTIVE",
    "_RetrieverSearcher",
    "_SYSTEM_PROMPT",
    "_intent_title",
    "_is_design_stage",
    "_resumption_messages",
    "design_directive_message",
    "design_seed_message",
    "knowledge_seed_message",
    "make_knowledge_search_handler",
    "register_invoke_skill_tool",
    "suggested_skill_message",
]
