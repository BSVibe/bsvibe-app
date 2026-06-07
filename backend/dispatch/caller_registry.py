"""Caller registry — the single source of truth for ``caller_id`` (Lift E1).

A *caller* is any code site that invokes an LLM through the dispatch
mechanism: knowledge ingest's compile pass, an agent-loop plan/act turn,
the frame stage, a judge, the canonicalization extractor, etc. Each one
declares an opaque, stable ``caller_id`` plus the adapter methods it
requires. The resolver matches the ``caller_id`` against the user's
:class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow` set;
rule creation cross-checks ``required_methods`` against the
:class:`~backend.dispatch.adapter.ModelAccountAdapter`'s
``supported_methods`` so an incompatible binding is rejected at write
time, never silently at dispatch.

Two sources are merged at lookup:

* **Static (this module)** — the core call sites that ship with bsvibe-app.
  Stable ids, version-controlled, code-reviewed.
* **Dynamic (skills)** — per-workspace skills loaded via
  :class:`~backend.extensions.skill.loader.SkillLoader` get a synthetic
  ``caller_id == f"skill.{name}"``.

Only E1's static surface is implemented today; the dynamic side is a thin
helper. Both sources expose the same :class:`CallerSpec` shape, so the
resolver does not have to discriminate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "CALLER_AGENT_LOOP_ACT",
    "CALLER_AGENT_LOOP_PLAN",
    "CALLER_FRAME",
    "CALLER_JUDGE",
    "CALLER_KNOWLEDGE_CANONICALIZATION",
    "CALLER_KNOWLEDGE_INGEST",
    "CALLER_KNOWLEDGE_QUERY",
    "CALLER_SETTLE_EXTRACT",
    "KNOWN_CALLERS",
    "SKILL_CALLER_PREFIX",
    "CallerSpec",
    "get_caller_spec",
    "list_all_callers",
]

# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallerSpec:
    """A call site's contract with the dispatch resolver.

    ``caller_id`` is the opaque identifier a RunRoutingRule matches on.
    Stable across versions — changing one is a routing-rule migration.

    ``required_methods`` is the set of adapter methods the call site will
    invoke. Only ``"chat"`` exists in E1; ``"execute"`` is reserved for a
    future verb. Rule creation rejects a binding whose target adapter does
    not support every required method (validated at write time, not at
    dispatch).

    ``description`` is for operator-facing surfaces — settings UIs that
    list callers, audit logs that name them, MCP tool descriptions.

    ``default_timeout_s`` (Lift E9) is the per-caller chat timeout
    override (seconds). ``None`` means "use ``settings.executor_task_timeout_s``"
    — keeps long-running coding-agent callers (``workflow.agent_loop.act``,
    5-15 minutes per turn) on the default while letting chat-shaped callers
    (knowledge ingest, frame, judge — 10-60 s when the worker is healthy)
    fail fast when the worker dies mid-task. Without this, one global
    1800 s default hammered every caller and stalled bsvibe-app's ~50-chunk
    bootstrap for a wall-clock day on a single hung chunk.
    """

    caller_id: str
    required_methods: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))
    description: str = ""
    default_timeout_s: float | None = None


# ---------------------------------------------------------------------------
# Static core registry
# ---------------------------------------------------------------------------

#: Knowledge ingest's compile pass — :class:`backend.knowledge.ingest.ingest_compiler.IngestCompiler`.
CALLER_KNOWLEDGE_INGEST = "knowledge.ingest"
#: Knowledge query / answer orchestrator — single-turn QA over the workspace ontology.
CALLER_KNOWLEDGE_QUERY = "knowledge.query"
#: BSage canonicalization mutation extractor.
CALLER_KNOWLEDGE_CANONICALIZATION = "knowledge.canonicalization"
#: Frame stage — cheap completion that classifies the run + matches a skill.
CALLER_FRAME = "workflow.frame"
#: Agent loop plan turn (heavy reasoning step).
CALLER_AGENT_LOOP_PLAN = "workflow.agent_loop.plan"
#: Agent loop act turn (tool-emitting step).
CALLER_AGENT_LOOP_ACT = "workflow.agent_loop.act"
#: Judge / verifier turn for executor verification path.
CALLER_JUDGE = "workflow.judge"
#: Settle worker's entity extractor — populates the ontology from finished runs.
CALLER_SETTLE_EXTRACT = "workflow.settle.extract"

#: Prefix for the dynamic skill caller_id namespace. ``skill.<name>``.
SKILL_CALLER_PREFIX = "skill."

#: The core call sites that ship with bsvibe-app. New entries land in the
#: same lift that introduces the call site — never speculatively.
KNOWN_CALLERS: dict[str, CallerSpec] = {
    CALLER_KNOWLEDGE_INGEST: CallerSpec(
        caller_id=CALLER_KNOWLEDGE_INGEST,
        required_methods=frozenset({"chat"}),
        description=(
            "Knowledge ingest compile pass — one structured-output chat call per "
            "chunk that produces the JSON garden-action plan."
        ),
        # 3 min — a chunk's structured-output call is normally 10-60 s. A
        # bsvibe-app bootstrap is ~50 chunks; one stuck chunk at the legacy
        # 30 min default could waste a wall-clock day.
        default_timeout_s=180.0,
    ),
    CALLER_KNOWLEDGE_QUERY: CallerSpec(
        caller_id=CALLER_KNOWLEDGE_QUERY,
        required_methods=frozenset({"chat"}),
        description=(
            "Knowledge query answerer — single chat call over the workspace "
            "ontology when the frame classified the ask as knowledge_only."
        ),
        # 1 min — interactive query, founder is waiting for the answer.
        default_timeout_s=60.0,
    ),
    CALLER_KNOWLEDGE_CANONICALIZATION: CallerSpec(
        caller_id=CALLER_KNOWLEDGE_CANONICALIZATION,
        required_methods=frozenset({"chat"}),
        description=(
            "BSage canonicalization mutation extractor — proposes cannot-link / "
            "must-link decisions over the canonical graph."
        ),
        default_timeout_s=180.0,
    ),
    CALLER_FRAME: CallerSpec(
        caller_id=CALLER_FRAME,
        required_methods=frozenset({"chat"}),
        description=(
            "Frame stage — cheap classify+skill-match completion before the agent loop dispatches."
        ),
        default_timeout_s=180.0,
    ),
    CALLER_AGENT_LOOP_PLAN: CallerSpec(
        caller_id=CALLER_AGENT_LOOP_PLAN,
        required_methods=frozenset({"chat"}),
        description=(
            "Agent loop plan turn — heavy reasoning step that decides the next "
            "action without emitting tool calls."
        ),
        # 5 min — plan turn is heavier than a chat-shaped call but doesn't
        # run the full `claude --print` subprocess.
        default_timeout_s=300.0,
    ),
    CALLER_AGENT_LOOP_ACT: CallerSpec(
        caller_id=CALLER_AGENT_LOOP_ACT,
        required_methods=frozenset({"chat"}),
        description=(
            "Agent loop act turn — the tool-emitting turn whose response can "
            "include tool_calls the workflow then dispatches."
        ),
        # Genuinely long — a tool-emitting turn runs `claude --print` /
        # `codex -p` / `opencode -p` on a real coding task. Leave at None
        # so it picks up the settings default of 1800 s.
        default_timeout_s=None,
    ),
    CALLER_JUDGE: CallerSpec(
        caller_id=CALLER_JUDGE,
        required_methods=frozenset({"chat"}),
        description=(
            "Judge / verifier — grades a candidate deliverable against the run's "
            "verification contract."
        ),
        default_timeout_s=180.0,
    ),
    CALLER_SETTLE_EXTRACT: CallerSpec(
        caller_id=CALLER_SETTLE_EXTRACT,
        required_methods=frozenset({"chat"}),
        description=(
            "Settle worker's entity extractor — single chat call over the "
            "verified deliverable's transcript to populate the ontology."
        ),
        default_timeout_s=180.0,
    ),
}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_caller_spec(caller_id: str, *, skill_names: Iterable[str] | None = None) -> CallerSpec:
    """Return the :class:`CallerSpec` for ``caller_id``.

    Lookup precedence:

    1. Static :data:`KNOWN_CALLERS`.
    2. Dynamic skill namespace (``skill.<name>``) — when ``skill_names`` is
       provided AND the ``<name>`` is in it.

    Unknown ids raise :class:`KeyError` so the resolver can surface the
    miss as a validation error (rather than silently picking a fallback).
    """
    spec = KNOWN_CALLERS.get(caller_id)
    if spec is not None:
        return spec
    if caller_id.startswith(SKILL_CALLER_PREFIX):
        name = caller_id[len(SKILL_CALLER_PREFIX) :]
        if skill_names is not None and name in set(skill_names):
            return CallerSpec(
                caller_id=caller_id,
                required_methods=frozenset({"chat"}),
                description=f"Skill {name!r} — workspace-managed call site.",
            )
    raise KeyError(f"unknown caller_id {caller_id!r}")


def list_all_callers(*, skill_names: Iterable[str] | None = None) -> list[CallerSpec]:
    """All static callers plus the per-workspace skill callers.

    ``skill_names`` is the list the caller resolved from the workspace's
    :class:`~backend.extensions.skill.loader.SkillLoader.registry`. We do
    NOT reach into the skill loader from here — the registry is
    workspace-scoped and stays the caller's responsibility, so the
    dispatch context (a leaf) does not depend on the skill loader's
    construction site.
    """
    out: list[CallerSpec] = list(KNOWN_CALLERS.values())
    for name in skill_names or ():
        out.append(
            CallerSpec(
                caller_id=f"{SKILL_CALLER_PREFIX}{name}",
                required_methods=frozenset({"chat"}),
                description=f"Skill {name!r} — workspace-managed call site.",
            )
        )
    return out
