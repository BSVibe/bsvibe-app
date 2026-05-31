"""Audit events emitted from the agent-loop / executor execution paths (B15).

Per Workflow §4 the ``audit`` stream is meant to be the always-on observability
backbone across all steps, not just the chat-completions surface. Before B15
only :mod:`backend.api.litellm_hook.audit_events` was wired, so the supervisor
outbox (drained by :class:`backend.workflow.infrastructure.workers.relay_worker.RelayWorker`) saw no
run-level events at all — :class:`backend.execution.db.ExecutionRunActivity`
rows existed in the DB but the audit stream was blind to them.

B15 adds a small set of high-signal events covering run lifecycle, each LLM
turn, each tool call, verification outcomes, decision raise + resolve, and the
loop terminal. Payloads are deliberately TINY (ids + small summaries — never
the full LLM content) so the outbox stays cheap; the rich payload still lives
on ``ExecutionRunActivity`` rows for forensics.

Every event mirrors the chat-completions pattern (small Pydantic class extending
:class:`AuditEventBase`, ``DEFAULT_EVENT_TYPE`` pinning the wire shape) so the
relay loop drains them through the SAME outbox + delivery path — no new stream,
no new schema. Emission goes through
:func:`plugin.audit.service.safe_emit` which already swallows
failures, so an audit hiccup can NEVER break a run.
"""

from __future__ import annotations

from typing import ClassVar

from plugin.audit.events import AuditEventBase


class RunStarted(AuditEventBase):
    """An execution run started driving (native loop OR executor dispatch)."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.run.started"


class LlmTurn(AuditEventBase):
    """One LLM completion round inside the native agent loop."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.llm.turn"


class ToolCall(AuditEventBase):
    """One tool invocation inside the native agent loop."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.tool.call"


class VerifyRun(AuditEventBase):
    """A verification contract was run; payload carries the outcome."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.verify.run"


class DecisionPending(AuditEventBase):
    """A blocking :class:`Decision` was raised — the run is paused for the
    founder (ask_user_question, verification_failed, human_review_required,
    no_executor_worker_available, no_executor_dispatch_transport,
    connector_action_approval, …)."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.decision.pending"


class DecisionResolved(AuditEventBase):
    """A founder resolved a pending checkpoint via /api/v1/checkpoints."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.decision.resolved"


class LoopTerminal(AuditEventBase):
    """The loop reached a terminal outcome — ``verified`` / ``needs_decision``
    / ``system_error``. One per run-attempt; the founder-facing closing event."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "execution.loop.terminal"


__all__ = [
    "DecisionPending",
    "DecisionResolved",
    "LlmTurn",
    "LoopTerminal",
    "RunStarted",
    "ToolCall",
    "VerifyRun",
]
