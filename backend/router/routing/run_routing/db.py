"""RunRoutingRuleRow — a per-workspace RUN routing rule (Phase 1).

Distinct from the gateway's account-scoped chat/model rules
(:mod:`backend.router.rules`, table ``routing_rules`` — which picks the LLM
*model within a native run* via the litellm hook). Run routing is a layer
ABOVE that: it picks WHICH ModelAccount (native vs executor CLI) drives a run,
keyed on the run's framed signals. Hence a separate table + context.

``conditions`` is a JSON list of ``{field, operator, value, negate}`` objects
(see :mod:`backend.router.routing.run_routing.engine` for the evaluation semantics). ``target``
is a ModelAccount selector — matched against an active account's
``litellm_model`` (e.g. ``"executor/codex"`` or a native model name).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base


class RunRoutingRuleRow(Base):
    __tablename__ = "run_routing_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Lift E2 — the canonical caller_id this rule routes. Required for
    # any non-default (non catch-all) rule; the default rule (is_default
    # AND empty conditions AND null caller_id) catches everything else.
    # Validated at write time against ``backend.dispatch.caller_registry``
    # so a typo never persists as a no-op rule.
    caller_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Lower priority evaluated FIRST (BSGateway semantics — ascending sort).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The fallback rule used when no non-default rule matches.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ModelAccount selector — matched against an active account's litellm_model.
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    # Lift N5 — the founder's ORIGINAL plain-language CONDITION phrase this rule
    # was compiled from ("복잡한 작업", "마케팅 관련"). NULL for legacy /
    # directly-structured rules. Display + edit metadata: the compiled
    # caller_id/conditions (not this text) is what the resolver evaluates.
    source_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # list[{field, operator, value, negate}] — AND-ed at evaluation time.
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_run_routing_rule_name"),)


__all__ = ["RunRoutingRuleRow"]
