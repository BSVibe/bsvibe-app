"""``decompose_request`` — single LLM call that turns a
:class:`ProjectContext` into a list of :class:`WorkStepDraft`.

Same model, same auth, same tool surface as the worker LLM (it just
runs with ``tools=None`` because this is reasoning, not execution).
The point is *not* to add a new role: it's the same work LLM doing its
first turn of thinking before the WorkStep loop starts.

Failure modes are silent and fall back to a single-step plan that
mirrors the Request intent. This is the same shape ``plan_and_dispatch_request``
used in G9, so a flaky decomposer call degrades the system to the
G9 behavior — never to a crash.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

# Narrow Protocol stub until BSNexus' full ExecutorClient lands. See
# backend.execution._executor_protocol for the contract.
from backend.execution._executor_protocol import ExecutorClient
from backend.execution.planning.context import ProjectContext
from backend.execution.planning.prompts import render_decomposer_messages
from backend.execution.work_steps import WorkStepDraft

logger = structlog.get_logger(__name__)


MAX_STEPS = 6
_PARSE_RETRIES = 1  # one retry on parse failure, then fall back
_WORK_STEP_NAME_MAX = 80

# Heuristic temperature schedule for the decomposer call, indexed by
# attempt. The decomposer is a *structuring decision*, not creative
# generation — at the worker's default sampling temperature the same
# Direction yielded n_steps in {1,1,1,1,3,6} across runs. Attempt 0
# runs near-greedy for a stable, repeatable plan; the parse-failure
# retry steps the temperature up so the re-sample is actually a
# different draw (a retry at the same low temp would just repeat the
# unparseable output). Index past the end clamps to the last value.
_DECOMPOSE_TEMPERATURES = (0.2, 0.6)

# Fenced ```json block — capture either a [...] array or a {...} object.
_FENCE_RE = re.compile(r"```(?:json)?\s*([\[{][\s\S]*[\]}])\s*```", re.IGNORECASE)
# Keys a wrapper object might nest the step list under.
_STEP_LIST_KEYS = ("steps", "plan", "worksteps", "work_steps", "tasks")


async def decompose_request(
    ctx: ProjectContext,
    *,
    executor: ExecutorClient,
    model: str,
    metadata: dict[str, Any] | None = None,
) -> list[WorkStepDraft]:
    """Return a list of WorkStepDrafts derived from ``ctx``.

    ``metadata`` is forwarded to ``executor.execute`` so the wire
    contracts of each path are satisfied (DirectLLMAdapter requires
    ``tenant_id`` + ``run_id``; BSGateway requires ``tenant_id``).
    Surfaced as a bug in the first prod dogfood: a missing-metadata
    call raised inside DirectLLMAdapter and silently triggered the
    single-step fallback. Production callers MUST pass both keys —
    a synthetic ``decompose:<request_id>`` works fine for ``run_id``
    since this LLM call doesn't have a tracked RunAttempt yet.

    Always returns at least one draft. If the LLM call fails, returns
    a single-step plan that mirrors the Request intent (G9 behavior).
    """
    intent = ctx.request_intent.strip()
    fallback = [_single_step_fallback(intent)]
    if not intent:
        return fallback

    messages = render_decomposer_messages(ctx, max_steps=MAX_STEPS)

    merged_metadata: dict[str, Any] = {"phase": "decompose", **(metadata or {})}

    text = await _call_with_parse_retry(
        executor=executor, model=model, messages=messages, metadata=merged_metadata
    )
    if text is None:
        logger.info(
            "decompose_fallback", reason="llm_unavailable_or_unparseable", intent=intent[:80]
        )
        return fallback

    drafts = _parse_drafts(text)
    if not drafts:
        logger.info("decompose_fallback", reason="no_valid_steps", intent=intent[:80])
        return fallback

    if len(drafts) > MAX_STEPS:
        drafts = _truncate_with_followup_marker(drafts)

    logger.info("decompose_succeeded", n_steps=len(drafts), intent=intent[:80])
    return drafts


async def _call_with_parse_retry(
    *,
    executor: ExecutorClient,
    model: str,
    messages: list[dict[str, str]],
    metadata: dict[str, Any],
) -> str | None:
    """Invoke the executor up to ``_PARSE_RETRIES + 1`` times. Returns
    the first text response whose JSON parses, or None if every attempt
    raises / returns blank / yields no valid JSON.
    """
    last_text: str | None = None
    for attempt in range(_PARSE_RETRIES + 1):
        temperature = _DECOMPOSE_TEMPERATURES[min(attempt, len(_DECOMPOSE_TEMPERATURES) - 1)]
        try:
            result = await executor.execute(
                messages=messages,
                metadata=metadata,
                model=model,
                tools=None,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 — decomposer must never bubble
            logger.warning("decompose_executor_error", attempt=attempt, error=str(exc))
            return None
        text = (result.get("output_ref") or "").strip()
        if not text:
            logger.warning("decompose_empty_output", attempt=attempt)
            continue
        last_text = text
        # Parseable step list → done. Otherwise retry: the next attempt
        # runs at a higher temperature (see _DECOMPOSE_TEMPERATURES) so
        # the re-sample is genuinely different.
        if _extract_step_list(text) is not None:
            return text
    return last_text  # may still be unparseable — caller falls back


def _json_candidate(text: str) -> str | None:
    """Extract the outermost JSON value substring from ``text``.

    Prefers a ```json fenced``` block; otherwise scans from the first
    ``[`` or ``{`` with balanced-bracket matching (string-aware), which
    grabs the *outer* structure — a plain ``[...]``/``{...}`` regex
    grabs whichever bracket pair appears first and would mis-capture an
    inner array (e.g. a step object's ``expected_outputs``)."""
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1)
    start = next((i for i, ch in enumerate(text) if ch in "[{"), None)
    if start is None:
        return None
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_step_list(text: str) -> list[Any] | None:
    """Return a list of step-candidate values from ``text``, or None.

    Normalizes the three shapes qwen3-coder actually emits:
      - a JSON array ``[{...}, ...]`` (the asked-for shape)
      - a bare single-step object ``{name, objective, ...}`` — emitted
        ~3/8 of the time for one-step plans; wrapped into ``[obj]``
      - a wrapper object ``{"steps": [...]}`` / ``{"plan": [...]}``
    """
    candidate = _json_candidate(text)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # A bare single-step object.
        if "name" in parsed and "objective" in parsed:
            return [parsed]
        # A wrapper object nesting the list under a known key.
        for key in _STEP_LIST_KEYS:
            value = parsed.get(key)
            if isinstance(value, list):
                return value
    return None


def _parse_drafts(text: str) -> list[WorkStepDraft]:
    """Parse ``text`` into WorkStepDrafts. Invalid entries are dropped;
    a fully-invalid array returns ``[]`` and the caller falls back to
    the single-step plan."""
    raw = _extract_step_list(text)
    if raw is None:
        return []
    drafts: list[WorkStepDraft] = []
    for entry in raw:
        draft = _coerce_draft(entry)
        if draft is not None:
            drafts.append(draft)
    return drafts


def _coerce_draft(entry: Any) -> WorkStepDraft | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    objective = entry.get("objective")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(objective, str) or not objective.strip():
        return None
    expected = entry.get("expected_outputs", [])
    if not isinstance(expected, list):
        expected = []
    expected_strs = [str(item).strip() for item in expected if str(item).strip()]
    return WorkStepDraft(
        name=name.strip()[:_WORK_STEP_NAME_MAX],
        objective=objective.strip(),
        expected_outputs=expected_strs,
    )


def _truncate_with_followup_marker(drafts: list[WorkStepDraft]) -> list[WorkStepDraft]:
    """Cap to ``MAX_STEPS`` and rename the last step so the founder
    can see the Request still has uncovered scope."""
    head = drafts[: MAX_STEPS - 1]
    last_original = drafts[MAX_STEPS - 1]
    last = WorkStepDraft(
        name=f"follow-up split required — {last_original.name}"[:_WORK_STEP_NAME_MAX],
        objective=last_original.objective,
        expected_outputs=last_original.expected_outputs,
    )
    return [*head, last]


def _single_step_fallback(intent: str) -> WorkStepDraft:
    """G9-equivalent single-step plan: Request title becomes the step
    name, full intent the objective."""
    name = (intent.splitlines()[0] if intent else "Request")[:_WORK_STEP_NAME_MAX] or "Request"
    return WorkStepDraft(name=name, objective=intent or name, expected_outputs=[])
