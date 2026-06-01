"""RunRoutingRuleRepository Protocol — read/write seam for run-routing rules.

v8 D44/D45. The :class:`backend.router.routing.run_routing.db.RunRoutingRuleRow`
aggregate carries the per-workspace, priority-ordered run-routing rules — the
rule set the run resolver consults BEFORE the D2 tier default. Application
code calls this Protocol instead of issuing raw ``select(RunRoutingRuleRow)``.

Concrete impl lives in
:class:`backend.router.infrastructure.repositories.SqlAlchemyRunRoutingRuleRepository`.

Sort semantics: BSGateway-faithful ``priority`` ASCENDING — lower-priority
rules evaluate FIRST. The engine then short-circuits on the first match;
``is_default`` is the catch-all fallback. The ``has_any`` method exists so
the agent_runner Workflow→Router cross-reference (the design→impl handoff
gate) doesn't have to fetch the whole rule list just to ask "are there any?"
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.router.routing.run_routing.db import RunRoutingRuleRow


@runtime_checkable
class RunRoutingRuleRepository(Protocol):
    """Persistence seam for :class:`RunRoutingRuleRow`."""

    async def list_by_workspace(self, *, workspace_id: uuid.UUID) -> list[RunRoutingRuleRow]:
        """All rules for ``workspace_id`` ordered by ``priority`` ASC (lower first).

        Returns an empty list when no rules exist (the rule-less workspace
        falls back to D2 tier-default + legacy single-active resolution,
        not to a Decision). The engine filters ``is_active`` in-process so
        the caller can also surface inactive rules to admin UIs without a
        second query."""

    async def get(self, *, workspace_id: uuid.UUID, rule_id: uuid.UUID) -> RunRoutingRuleRow | None:
        """The rule with ``rule_id`` scoped to ``workspace_id``, or ``None``.

        Scoped (not a bare ``session.get(rule_id)``) so the REST delete path
        gets a clean 404 on cross-workspace access instead of leaking
        existence."""

    async def has_any(self, *, workspace_id: uuid.UUID) -> bool:
        """``True`` when ``workspace_id`` has at least one rule.

        Powers the Workflow→Router cross-reference in
        :meth:`backend.workflow.application.agent_runner.AgentRunner._workspace_has_routing_rules`
        — the design→impl handoff gate that opts into the rule-routed
        execution model. Implemented as a ``SELECT id … LIMIT 1`` so it
        stays O(1) for the gating check."""

    async def add(self, row: RunRoutingRuleRow) -> None:
        """Stage ``row`` for INSERT + flush.

        Like the other Router/Workflow Repository ``add`` methods, this
        flushes (so the caller observes ``IntegrityError`` from the
        ``(workspace_id, name)`` unique constraint synchronously) but does
        NOT commit — the caller owns the transaction boundary (D45)."""

    async def delete(self, row: RunRoutingRuleRow) -> None:
        """Stage ``row`` for DELETE + flush.

        The REST handler resolves the row via :meth:`get` (so workspace
        scoping is enforced before the delete) and passes the row here so
        the seam doesn't have to re-query."""


__all__ = ["RunRoutingRuleRepository"]
