"""Executor prompt assembly (B8) — context-rich framing for the CLI worker.

Extracted from :mod:`backend.executors.orchestrator` in Lift D (§17.8 4-file
split). Pure, synchronous helpers — the coordinator does the async canon
retrieval + design-spec read and threads results in here.

B8 brings CLI dispatch up to native parity: instead of shipping a bare 512-char
intent with an EMPTY system prompt, the orchestrator frames a context-rich
prompt (intent + relevant canon + founder-resolved decisions) + a real engineer
system prompt. Caps respect the local-model generation budget.

D1b — DESIGN-stage runs of a ``design_then_impl`` pipeline are told to write a
spec instead of finished code (prevents the impl-stage no-op merge the
2026-05-28 dogfood found).
"""

from __future__ import annotations

import uuid
from typing import Any

from backend.execution.db import ExecutionRun

# The intent is the REAL instruction (not a title), so the legacy 512-char cap
# is lifted to a few KB — still bounded so a runaway intent never blows the
# generation budget.
_INTENT_MAX_CHARS = 8_000
# Canon folded into the prompt as "Relevant established patterns" — top-N
# statements, each clamped (mirrors the native B6 knowledge seed: 5 × 500).
_KNOWLEDGE_MAX_RESULTS = 5
_KNOWLEDGE_MAX_CHARS_PER_STATEMENT = 500

# The executor system prompt — engineer guidance for a delegated CLI agent that
# runs its OWN tool loop (unlike the native loop, the CLI owns plan→act→verify).
# Adapted from the native ``_SYSTEM_PROMPT`` intent (do the framed work, produce
# the artifacts) but fitted to a self-driving CLI rather than the loop's
# declare_verification-gated tool protocol.
_EXECUTOR_SYSTEM_PROMPT = (
    "You are an autonomous software engineer executing a delegated task inside a "
    "working directory. Read the framed task, then use your own tools to inspect "
    "and change files until the work is complete. Produce the concrete "
    "artifacts the task asks for — write real files, run the relevant "
    "tests/lint, and leave the work in a verifiable state (your output is "
    "checked against a verification contract afterwards). Honor any established "
    "patterns and founder decisions included in the task. Do the work; do not "
    "ask for permission to proceed."
)

# D1b — when a run is the DESIGN stage of a ``design_then_impl`` pipeline, it
# must produce a SPECIFICATION (a concise markdown spec a later impl stage
# implements), NOT finished code. Today the design run gets the generic work
# prompt above with nothing telling it to spec rather than build, so it builds
# working code the impl stage then regenerates — a no-op merge (2026-05-28
# dogfood). This directive, prepended to the design run's work prompt, redirects
# it to spec. One concise instruction block (respect the local-model generation
# budget — not a heavy multi-section template). The ``single`` + ``impl`` work
# prompts never receive it (impl IMPLEMENTS the spec, so telling it to spec
# would reintroduce the no-op).
_DESIGN_SPEC_DIRECTIVE = (
    "THIS IS THE DESIGN STAGE. Write ONE concise markdown specification — do NOT "
    "implement it and do NOT write working code; a later implementation stage "
    "will. The spec MUST cover: Goal (what to build and why), "
    "Interface/Contract (the public API, signatures, inputs/outputs), File "
    "layout (the files to create and what each holds), and Acceptance criteria "
    "(observable conditions that prove the implementation is correct). Keep it "
    "tight and implementable; output only the spec."
)


def _intent_text(run: ExecutionRun, *, max_chars: int = 512) -> str:
    """The run's stable intent — the same input the native loop seeds with
    (``backend.execution.orchestrator._intent_title``).

    ``max_chars`` defaults to the legacy 512 cap used for the WorkStep title and
    canon-retrieval signal; the framed dispatch prompt lifts it to
    :data:`_INTENT_MAX_CHARS` (the intent is the real instruction there)."""
    payload = run.payload or {}
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:max_chars]


def _resolved_decisions(run: ExecutionRun) -> list[tuple[str, str]]:
    """Extract ``(question, answer)`` pairs from ``run.payload["resolved_decisions"]``.

    Same data the native ``_resumption_messages`` uses (appended by the
    checkpoints resolve endpoint). Entries without an answer / malformed entries
    are skipped. Always returns a list (never raises) — graceful for a resumed
    executor run with no decisions."""
    payload = run.payload or {}
    resolved = payload.get("resolved_decisions") if isinstance(payload, dict) else None
    if not isinstance(resolved, list):
        return []
    pairs: list[tuple[str, str]] = []
    for entry in resolved:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if not answer:
            continue
        pairs.append((question, answer))
    return pairs


def _executor_system_prompt() -> str:
    """The engineer system prompt for the delegated CLI agent (B8)."""
    return _EXECUTOR_SYSTEM_PROMPT


def _is_design_stage(run: ExecutionRun) -> bool:
    """D1b — True when this run is the DESIGN stage of a ``design_then_impl``
    pipeline (so its work prompt is told to spec, not build).

    The condition mirrors routing's ``_derive_stage``: the FIRST run of a
    ``design_then_impl`` pipeline never carries an explicit ``stage`` (the
    AgentRunner chains impl off the frame's pipeline signal, not a stage
    column), so an unset / non-``impl`` stage on a ``design_then_impl`` run IS
    the design stage. The spawned implementation run carries ``stage="impl"`` and
    is excluded — it implements the spec. Any other pipeline (``single`` / no
    frame) is excluded. Tolerant of a missing/odd payload."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    raw_frame = payload.get("frame")
    frame = raw_frame if isinstance(raw_frame, dict) else {}
    if frame.get("pipeline") != "design_then_impl":
        return False
    return payload.get("stage") != "impl"


def _assemble_executor_prompt(
    run: ExecutionRun, *, statements: list[str], design_context: str | None = None
) -> str:
    """Frame the context-rich CLI prompt: intent + canon + resolved decisions
    + (P1-L2b) the prior design stage's spec.

    Pure + synchronous (testable in isolation) — the caller does the async canon
    retrieval + design-spec read and passes the results. Sections that have no
    content are omitted entirely (no empty headers): an empty-knowledge,
    no-decisions run yields just the intent. Caps applied: the intent to
    :data:`_INTENT_MAX_CHARS`, canon to :data:`_KNOWLEDGE_MAX_RESULTS` × clamped
    statements (respect the local-model generation budget)."""
    parts: list[str] = [_intent_text(run, max_chars=_INTENT_MAX_CHARS)]

    # D1b — a DESIGN-stage run is told to write a spec, not build. Prepended
    # (after the intent) so it frames the whole task. Excludes single + impl.
    if _is_design_stage(run):
        parts.append(_DESIGN_SPEC_DIRECTIVE)

    if design_context:
        parts.append(design_context)

    cleaned = [
        s.strip()[:_KNOWLEDGE_MAX_CHARS_PER_STATEMENT] for s in statements if s and s.strip()
    ][:_KNOWLEDGE_MAX_RESULTS]
    if cleaned:
        body = "\n".join(f"- {s}" for s in cleaned)
        parts.append("Relevant established patterns for this workspace:\n" + body)

    decisions = _resolved_decisions(run)
    if decisions:
        lines = [f"- Q: {q} A: {a}" for q, a in decisions]
        parts.append(
            "The founder resolved these prior questions — honor them:\n" + "\n".join(lines)
        )

    return "\n\n".join(parts)


def _parse_uuid(value: Any) -> uuid.UUID | None:
    """Best-effort parse of a stored ``worker_id`` tag (always a str in JSON)."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


__all__ = [
    "_DESIGN_SPEC_DIRECTIVE",
    "_EXECUTOR_SYSTEM_PROMPT",
    "_INTENT_MAX_CHARS",
    "_KNOWLEDGE_MAX_CHARS_PER_STATEMENT",
    "_KNOWLEDGE_MAX_RESULTS",
    "_assemble_executor_prompt",
    "_executor_system_prompt",
    "_intent_text",
    "_is_design_stage",
    "_parse_uuid",
    "_resolved_decisions",
]
