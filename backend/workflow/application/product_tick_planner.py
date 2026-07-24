"""ProductTickPlanner — a dedicated planner for autonomous product ticks.

A scheduled ``product_tick`` fires with only a cadence; the emitter seeds a
FIXED localized meta-instruction (:func:`backend.schedule.domain.product_tick.product_tick_instruction`)
as the run's first turn, and the agent then self-decides the next action with no
seeded product / knowledge / history context — shallow.

This planner replaces that self-decision at frame time. It STRUCTURALLY reads

* the product (name, repo, free-form ``metadata`` — the goal / lifecycle live
  there; there is deliberately NO goal/stage column),
* the product's recent run history (what has already been attempted), and
* the workspace's accumulated knowledge (via the canon retriever),

then composes ONE in-process cheap-LLM call (the ``CALLER_FRAME`` tier, exactly
like :class:`~backend.workflow.application.report_narrative.ReportNarrativeService`)
that returns a CONCRETE next-action :class:`TickPlan`. The worker overrides the
framing intent with ``plan.instruction`` so framing classifies the real task,
and stashes the plan as glass-box provenance on the run.

It NEVER raises to the caller: a missing product, a workspace mismatch, an
unresolved route, a retriever / DB / LLM hiccup, or unparseable output all
degrade to ``None`` so the tick still runs on the static meta-instruction
fallback.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.dispatch.adapter import ExecutorCapacitySaturated
from backend.dispatch.caller_registry import CALLER_FRAME
from backend.identity.output_language import language_directive
from backend.identity.workspaces_db import ProductRow, load_workspace_language
from backend.knowledge.retrieval.answer_grounding import build_canon_retriever
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.infrastructure.repositories import SqlAlchemyRunRepository

logger = structlog.get_logger(__name__)

#: How many recent runs to summarize into the prompt.
_MAX_RUNS = 10
#: How many knowledge snippets to fold into the prompt.
_MAX_KNOWLEDGE_SNIPPETS = 6
#: Per-item budgets so a verbose product / note can't blow the framing budget.
_META_MAX_CHARS = 1500
_SNIPPET_MAX_CHARS = 600
_INTENT_MAX_CHARS = 160
#: Backstop on the composed instruction (the prompt asks for 1-2 sentences).
_INSTRUCTION_MAX_CHARS = 600

#: Free-form metadata keys we treat as the product's "goal" for the retrieval
#: signals. Order = precedence. Absent → the metadata contributes only its name.
_GOAL_KEYS = ("goal", "objective", "mission", "north_star", "lifecycle", "stage")

_PLANNER_SYSTEM_PROMPT = (
    "You are the autonomous planner for a product. A scheduled tick has fired: "
    "the founder set only the cadence, and YOU decide the single most valuable "
    "NEXT action to advance THIS product through its lifecycle. You are given the "
    "product's name, its free-form metadata (which holds the goal / lifecycle), "
    "its recent run history (what was already attempted), and the workspace's "
    "accumulated knowledge. Choose ONE concrete, shippable next task — prefer "
    "progress over analysis, and do NOT repeat work the run history already "
    "covers. Respond with ONE JSON object (no prose, no code fences):\n"
    '  "instruction": a CONCISE imperative directive (1-2 sentences) telling the '
    "agent exactly what single task to do next,\n"
    '  "rationale": a one-line why, grounded in the product state / history / '
    "knowledge above.\n"
    "The instruction must be a single focused task, not a plan or a list."
)


@dataclass(slots=True)
class TickPlan:
    """A concrete next-action decision for a product tick.

    ``instruction`` becomes the run's framing intent (its glass-box intent);
    ``rationale`` is the one-line why the founder sees as provenance.
    """

    instruction: str
    rationale: str


class ProductTickPlanner:
    """Compose a concrete next-action plan for an autonomous product tick."""

    def __init__(self, session: AsyncSession, *, settings: Settings, redis: Any = None) -> None:
        self._session = session
        self._settings = settings
        # ``CALLER_FRAME`` may resolve an EXECUTOR account whose adapter needs the
        # dispatch redis; ``None`` keeps a LiteLLM-account workspace working and
        # soft-fails an executor-only workspace to ``None`` (→ static fallback),
        # exactly like ReportNarrativeService.
        self._redis = redis

    async def plan(self, *, workspace_id: uuid.UUID, product_id: uuid.UUID) -> TickPlan | None:
        """A concrete :class:`TickPlan`, or ``None`` to fall back to the static
        meta-instruction. NEVER raises — any failure degrades to ``None``."""
        try:
            return await self._plan(workspace_id=workspace_id, product_id=product_id)
        except ExecutorCapacitySaturated:
            # Saturation is NOT a planner hiccup to swallow: the shared worker is
            # busy, so this tick must YIELD BACK (leave the run OPEN, retry next
            # poll) rather than fall through to ``None`` → a static-instruction
            # framing attempt that would hit the SAME saturated worker. Re-raise
            # so the AgentWorker's per-run loop catches it and continues.
            raise
        except Exception:  # noqa: BLE001 — a planner hiccup must never break the tick
            logger.warning(
                "product_tick_planner_failed",
                workspace_id=str(workspace_id),
                product_id=str(product_id),
                exc_info=True,
            )
            return None

    async def _plan(self, *, workspace_id: uuid.UUID, product_id: uuid.UUID) -> TickPlan | None:
        product = await self._session.get(ProductRow, product_id)
        if product is None or product.workspace_id != workspace_id:
            logger.info(
                "product_tick_planner_no_product",
                workspace_id=str(workspace_id),
                product_id=str(product_id),
            )
            return None

        resolved = await _resolve_via_caller(
            self._session,
            caller_id=CALLER_FRAME,
            workspace_id=workspace_id,
            settings=self._settings,
            redis=self._redis,
        )
        if resolved is None:
            # No route for the frame tier → no planner. The static
            # meta-instruction still runs.
            return None
        llm = ResolverLoopLlm(adapter=resolved.adapter)

        metadata = product.product_metadata if isinstance(product.product_metadata, dict) else {}
        history_summary = await self._history_summary(workspace_id, product_id)
        snippets = await self._knowledge_snippets(workspace_id, product, metadata)
        language = await load_workspace_language(self._session, workspace_id)

        user_message = _build_user_message(
            name=product.name,
            repo_url=product.repo_url,
            metadata=metadata,
            history_summary=history_summary,
            snippets=snippets,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT + language_directive(language)},
            {"role": "user", "content": user_message},
        ]
        turn = await llm.complete(messages=messages, tools=None)
        return _parse_plan(turn.content)

    async def _history_summary(self, workspace_id: uuid.UUID, product_id: uuid.UUID) -> str:
        run_repo = SqlAlchemyRunRepository(self._session)
        runs = await run_repo.list_by_product(workspace_id, product_id, limit=_MAX_RUNS)
        return _summarize_runs(runs)

    async def _knowledge_snippets(
        self, workspace_id: uuid.UUID, product: ProductRow, metadata: dict[str, Any]
    ) -> list[str]:
        signals = _retrieval_signals(product.name, metadata)
        retriever = build_canon_retriever(
            self._session, settings=self._settings, workspace_id=workspace_id
        )
        snippets = await retriever.retrieve_for_signals(signals)
        return list(snippets)[:_MAX_KNOWLEDGE_SNIPPETS]


def _goal_text(metadata: dict[str, Any]) -> str:
    """The goal-ish text out of the free-form metadata (best-effort)."""
    parts: list[str] = []
    for key in _GOAL_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts)


def _retrieval_signals(name: str, metadata: dict[str, Any]) -> str:
    """Signals for the canon retriever: the product name + goal-ish metadata."""
    goal = _goal_text(metadata)
    return " ".join(part for part in (name, goal) if part).strip() or name


def _summarize_runs(runs: list[Any]) -> str:
    """A compact newest-first bullet summary — status + intent, nothing heavy."""
    if not runs:
        return "(no runs yet)"
    lines: list[str] = []
    for run in runs:
        payload = run.payload if isinstance(run.payload, dict) else {}
        intent = payload.get("intent_text")
        intent_text = intent.strip() if isinstance(intent, str) and intent.strip() else "(untitled)"
        status = getattr(run.status, "value", str(run.status))
        lines.append(f"- [{status}] {intent_text[:_INTENT_MAX_CHARS]}")
    return "\n".join(lines)


def _build_user_message(
    *,
    name: str,
    repo_url: str | None,
    metadata: dict[str, Any],
    history_summary: str,
    snippets: list[str],
) -> str:
    parts: list[str] = [f"Product: {name}"]
    if repo_url:
        parts.append(f"Repo: {repo_url}")
    meta_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)[:_META_MAX_CHARS]
    parts.append(f"Metadata (free-form JSON — holds the goal / lifecycle):\n{meta_json}")
    parts.append(f"Recent run history (newest first):\n{history_summary}")
    if snippets:
        knowledge = "\n".join(f"- {s[:_SNIPPET_MAX_CHARS]}" for s in snippets)
    else:
        knowledge = "(none)"
    parts.append(f"Accumulated knowledge:\n{knowledge}")
    parts.append(
        "Decide the single most valuable next action for this product and return "
        "the JSON object described above."
    )
    return "\n\n".join(parts)


def _parse_plan(raw: str | None) -> TickPlan | None:
    """Parse the LLM's JSON plan, tolerating a code fence. ``None`` when the
    output is unparseable or the instruction is empty — the caller then falls
    back to the static meta-instruction."""
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(candidate[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    instruction = data.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return None
    rationale = data.get("rationale")
    rationale_text = rationale.strip() if isinstance(rationale, str) and rationale.strip() else ""
    return TickPlan(
        instruction=instruction.strip()[:_INSTRUCTION_MAX_CHARS],
        rationale=rationale_text[:_INSTRUCTION_MAX_CHARS],
    )


__all__ = ["ProductTickPlanner", "TickPlan"]
