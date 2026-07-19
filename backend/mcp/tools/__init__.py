"""MCP tool registrations — one entry point for the entire D2 surface."""

from __future__ import annotations

from backend.mcp.api import ToolRegistry
from backend.mcp.tools.account_tools import register_account_tools
from backend.mcp.tools.bindings_tools import register_bindings_tools
from backend.mcp.tools.connectors_tools import register_connectors_tools
from backend.mcp.tools.decisions_tools import register_decisions_tools
from backend.mcp.tools.direct_tools import register_direct_tools
from backend.mcp.tools.graph_tools import register_graph_tools
from backend.mcp.tools.inside_trust_tools import register_inside_trust_tools
from backend.mcp.tools.intents_tools import register_intents_tools
from backend.mcp.tools.knowledge_retraction_tools import (
    register_knowledge_retraction_tools,
)
from backend.mcp.tools.knowledge_tools import register_knowledge_tools
from backend.mcp.tools.model_accounts_tools import register_model_accounts_tools
from backend.mcp.tools.notifications_tools import register_notifications_tools
from backend.mcp.tools.run_routing_rules_tools import register_run_routing_rules_tools
from backend.mcp.tools.safe_mode_tools import register_safe_mode_tools
from backend.mcp.tools.schedule_tools import register_schedule_tools
from backend.mcp.tools.skills_tools import register_skills_tools
from backend.mcp.tools.work_registry import build_run_tool_registry, persist_tool_state
from backend.mcp.tools.work_tools import (
    RecordDeliverable,
    RecordQuestion,
    register_work_tools,
)
from backend.mcp.tools.workers_tools import register_workers_tools
from backend.mcp.tools.workflow_tools import register_workflow_tools
from backend.mcp.tools.workspace_tools import register_workspace_tools


def register_all_tools(
    registry: ToolRegistry,
    *,
    record_question: RecordQuestion | None = None,
    record_deliverable: RecordDeliverable | None = None,
) -> None:
    """Register every MCP tool onto ``registry``.

    Surfaces (D2 + D3a + D3b + D3c + D3d + E7):
    knowledge (5), workflow (7), safe-mode (3), direct (1),
    model-accounts (4 — D3a), connectors (5 — D3a),
    notifications (2 — D3a),
    bindings (4 — D3b), decisions (4 — D3b),
    knowledge-retraction (3 — D3c), skills (4 — D3c), workspace (2 — D3c),
    inside-trust (2 — D3d), account (2 — D3d),
    run-routing-rules (3 — E7), intents (3 — NL-native routing N2),
    schedules (4 — S2: create / list / delete / set_enabled).
    """
    register_knowledge_tools(registry)
    # T1 — the agent's REMOTE hands on a run (file/shell/declare/knowledge), bound to the
    # run's server-side worktree + sandbox. Only reachable with a run-scoped token (the one a
    # dispatched executor task carries), never with the founder's workspace token.
    if record_question is not None and record_deliverable is not None:
        # The two loop-owned effects are injected from the composition root: they live in the
        # workflow layer, and the deliverable one reaches ``backend.api.v1.live_events``, which
        # the MCP import contract forbids this context from importing. Absent them (a caller
        # that only wants the read/write surface), the work tools are simply not registered —
        # never registered-but-dead.
        register_work_tools(
            registry,
            registry_for_run=build_run_tool_registry,
            record_question=record_question,
            record_deliverable=record_deliverable,
            persist_state=persist_tool_state,
        )
    register_workflow_tools(registry)
    register_safe_mode_tools(registry)
    register_direct_tools(registry)
    register_model_accounts_tools(registry)
    register_connectors_tools(registry)
    register_notifications_tools(registry)
    register_bindings_tools(registry)
    register_decisions_tools(registry)
    register_run_routing_rules_tools(registry)
    register_intents_tools(registry)
    # Schedule authoring parity (S2) — mirror POST/GET/DELETE/PATCH /api/v1/schedules.
    register_schedule_tools(registry)
    register_knowledge_retraction_tools(registry)
    register_skills_tools(registry)
    register_workspace_tools(registry)
    register_inside_trust_tools(registry)
    register_account_tools(registry)
    register_workers_tools(registry)
    # Lift E20 — code-graph query surface (5 tools).
    register_graph_tools(registry)


__all__ = [
    "register_account_tools",
    "register_all_tools",
    "register_bindings_tools",
    "register_connectors_tools",
    "register_decisions_tools",
    "register_direct_tools",
    "register_graph_tools",
    "register_inside_trust_tools",
    "register_intents_tools",
    "register_knowledge_retraction_tools",
    "register_knowledge_tools",
    "register_model_accounts_tools",
    "register_notifications_tools",
    "register_run_routing_rules_tools",
    "register_safe_mode_tools",
    "register_schedule_tools",
    "register_skills_tools",
    "register_workers_tools",
    "register_workflow_tools",
    "register_workspace_tools",
]
