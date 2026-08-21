"""MCP tool registrations — one entry point for the entire D2 surface."""

from __future__ import annotations

from backend.mcp.api import ToolRegistry
from backend.mcp.tools.account_tools import register_account_tools
from backend.mcp.tools.bindings_tools import register_bindings_tools
from backend.mcp.tools.checkpoint_tools import register_checkpoint_tools
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
    RecordProgress,
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
    record_progress: RecordProgress | None = None,
) -> None:
    """Register every MCP tool onto ``registry``.

    이 docstring 은 표면별 툴 **개수를 세지 않는다.** 레지스트리가 SoT 이고,
    그 위에 손으로 센 요약을 얹으면 반드시 드리프트한다 — 2026-08-21 실측에서
    connectors 5→13 · safe-mode 3→7 · knowledge 5→8 · run-routing 3→6 으로
    어긋나 있었고 graph / products / runs / deliverables / workers 는 아예
    빠져 있었다 (요약 ~55 vs 실제 88).

    개수가 필요하면 세지 말고 물어라 — ``ToolRegistry`` 를 만들어 이 함수를
    돌린 뒤 등록된 이름을 읽으면 된다.
    """
    register_knowledge_tools(registry)
    # T1 — the agent's REMOTE hands on a run (file/shell/declare/knowledge), bound to the
    # run's server-side worktree + sandbox. Only reachable with a run-scoped token (the one a
    # dispatched executor task carries), never with the founder's workspace token.
    if (
        record_question is not None
        and record_deliverable is not None
        and record_progress is not None
    ):
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
            record_progress=record_progress,
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
    # Checkpoint resolution parity (C2) — mirror GET/POST /api/v1/checkpoints so
    # an away founder unblocks a paused run from an MCP client (ship is PWA-only).
    register_checkpoint_tools(registry)
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
    "register_checkpoint_tools",
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
