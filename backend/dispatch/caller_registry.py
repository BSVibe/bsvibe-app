"""Caller registry — the single source of truth for ``caller_id`` (Lift E1).

A *caller* is any code site that invokes an LLM through the dispatch
mechanism: knowledge ingest's compile pass, an agent-loop act turn,
the frame stage, a judge, the canonicalization extractor, etc. Each one
declares an opaque, stable ``caller_id`` plus the adapter methods it
requires. The resolver matches the ``caller_id`` against the user's
:class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow` set.

한때 ``required_methods`` / ``supported_methods`` 로 어댑터 호환성을 협상했지만,
선언된 값이 **전부 ``{"chat"}``** 이라 그 검사는 구조적으로 항상 통과했다 —
값이 하나뿐인 축은 협상이 아니다 (INV-7: 툴/메서드 표면이 곧 능력의 정의이고,
그 위의 enum 은 두 번째 소스다). 2026-08-21 에 걷어냈다. 두 번째 메서드가
실제로 생기면 그때가 그 축이 의미를 갖는 시점이다.

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
from dataclasses import dataclass

__all__ = [
    "CALLER_AGENT_LOOP_ACT",
    "CALLER_CHAT_COMPLETIONS",
    "CALLER_FRAME",
    "CALLER_JUDGE",
    "CALLER_KNOWLEDGE_CANONICALIZATION",
    "CALLER_KNOWLEDGE_INGEST",
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

    호출 지점이 실제로 부르는 어댑터 메서드는 ``chat`` 하나다 (E1 이후 그대로).
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

    ``yield_on_saturation`` marks run-drive callers whose run the
    :class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker`
    re-polls: on saturation (live workers exist but all are at capacity) the
    adapter raises :class:`~backend.dispatch.adapter.ExecutorCapacitySaturated`
    IMMEDIATELY (yield-back) instead of blocking the SHARED worker for up to
    ``settings.executor_capacity_wait_max_s`` (30 min) — leaving the run open
    and retrying on the next poll is strictly better than holding the worker
    slot + the run's DB lock and starving every other workspace's runs. Batch
    callers (``knowledge.ingest`` / ``knowledge.canonicalization``) that fan out
    via ``asyncio.gather`` CANNOT yield to a poll loop, so they keep the bounded
    capacity-wait (default ``False``).
    """

    caller_id: str
    description: str = ""
    default_timeout_s: float | None = None
    yield_on_saturation: bool = False


# ---------------------------------------------------------------------------
# Static core registry
# ---------------------------------------------------------------------------

#: Knowledge ingest's compile pass — :class:`backend.knowledge.ingest.ingest_compiler.IngestCompiler`.
CALLER_KNOWLEDGE_INGEST = "knowledge.ingest"
#: BSage canonicalization mutation extractor.
CALLER_KNOWLEDGE_CANONICALIZATION = "knowledge.canonicalization"
#: Frame stage — cheap completion that classifies the run + matches a skill.
CALLER_FRAME = "workflow.frame"
#: Agent loop act turn (tool-emitting step).
CALLER_AGENT_LOOP_ACT = "workflow.agent_loop.act"
#: Judge / verifier turn for executor verification path.
CALLER_JUDGE = "workflow.judge"
#: Settle worker's entity extractor — populates the ontology from finished runs.
CALLER_SETTLE_EXTRACT = "workflow.settle.extract"
#: External OpenAI-compatible ``/api/v1/chat/completions`` gateway. Lift 3 —
#: this surface now routes through the resolver like every internal caller
#: instead of demanding an explicit ``metadata.bsvibe_model_account_id``.
CALLER_CHAT_COMPLETIONS = "chat.completions"
#: The NL → run-routing-rules compiler (Lift 5) — one cheap chat call that turns
#: the founder's plain-language routing description into structured proposals.
CALLER_ROUTING_COMPILE = "routing.compile"

#: Prefix for the dynamic skill caller_id namespace. ``skill.<name>``.
SKILL_CALLER_PREFIX = "skill."

#: The core call sites that ship with bsvibe-app. New entries land in the
#: same lift that introduces the call site — never speculatively.
KNOWN_CALLERS: dict[str, CallerSpec] = {
    CALLER_KNOWLEDGE_INGEST: CallerSpec(
        caller_id=CALLER_KNOWLEDGE_INGEST,
        description=(
            "Knowledge ingest compile pass — one structured-output chat call per "
            "chunk that produces the JSON garden-action plan."
        ),
        # 10 min (Lift E14) — the 3 min cap (E9) was sized for small chunks
        # but big-repo bootstraps (bsvibe-app: 1134 chunks of 1377 file
        # artifacts) routinely send a 10-20 KB seed through the executor
        # adapter, and a single ``opencode run`` over that text takes
        # 5-16 min wall-clock. The dogfood symptom was a 3.6%
        # accelerating chunk-failure rate as the bootstrap hit those
        # big-file chunks; 10 min covers them while still failing fast on
        # a genuinely stuck chunk.
        default_timeout_s=600.0,
    ),
    CALLER_KNOWLEDGE_CANONICALIZATION: CallerSpec(
        caller_id=CALLER_KNOWLEDGE_CANONICALIZATION,
        description=(
            "BSage canonicalization mutation extractor — proposes cannot-link / "
            "must-link decisions over the canonical graph."
        ),
        # 10 min (Lift E14) — canonicalization passes fan out over the
        # workspace ontology and can run as long as a heavy ingest chunk.
        default_timeout_s=600.0,
    ),
    CALLER_FRAME: CallerSpec(
        caller_id=CALLER_FRAME,
        description=(
            "Frame stage — cheap classify+skill-match completion before the agent loop dispatches."
        ),
        # 5 min — frame is bounded reasoning, but the worker / executor
        # path is the same one knowledge.ingest uses, so give it the same
        # safety margin against worker queue contention.
        default_timeout_s=300.0,
        # Run-drive caller — the AgentWorker re-polls this run, so on
        # saturation the adapter yields back (raises immediately) instead of
        # blocking the shared worker for up to 30 min.
        yield_on_saturation=True,
    ),
    CALLER_AGENT_LOOP_ACT: CallerSpec(
        caller_id=CALLER_AGENT_LOOP_ACT,
        description=(
            "Agent loop act turn — the tool-emitting turn whose response can "
            "include tool_calls the workflow then dispatches."
        ),
        # Genuinely long — a tool-emitting turn runs `claude --print` /
        # `codex -p` / `opencode -p` on a real coding task that can include a
        # cold `uv sync` + a large repo's full pytest suite inline. Leave at
        # None so it picks up the settings default (3600 s / 1 h); the reaper
        # lease follows at 2×.
        default_timeout_s=None,
        # Run-drive caller — yields back on saturation (see CALLER_FRAME).
        yield_on_saturation=True,
    ),
    CALLER_JUDGE: CallerSpec(
        caller_id=CALLER_JUDGE,
        description=(
            "Judge / verifier — grades a candidate deliverable against the run's "
            "verification contract."
        ),
        default_timeout_s=300.0,
    ),
    CALLER_SETTLE_EXTRACT: CallerSpec(
        caller_id=CALLER_SETTLE_EXTRACT,
        description=(
            "Settle worker's entity extractor — single chat call over the "
            "verified deliverable's transcript to populate the ontology."
        ),
        default_timeout_s=300.0,
    ),
    CALLER_CHAT_COMPLETIONS: CallerSpec(
        caller_id=CALLER_CHAT_COMPLETIONS,
        description=(
            "External OpenAI-compatible /chat/completions gateway — routes to a "
            "ModelAccount by rule + workspace default, like the internal callers."
        ),
        # Interactive proxy surface — a caller is waiting on the response.
        default_timeout_s=120.0,
    ),
    CALLER_ROUTING_COMPILE: CallerSpec(
        caller_id=CALLER_ROUTING_COMPILE,
        description=(
            "NL → routing-rules compiler — one cheap chat call that turns a "
            "plain-language routing description into structured rule proposals."
        ),
        # Interactive authoring — the founder is waiting on the preview.
        default_timeout_s=120.0,
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
                description=f"Skill {name!r} — workspace-managed call site.",
            )
        )
    return out
