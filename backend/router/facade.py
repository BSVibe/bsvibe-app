# bsvibe:stable-internal — modifications require a design doc update.
# Owners: router/facade
"""Router context — facade Protocol (Lift A).

This module defines the public surface of the future Router context. No callers
are switched to it yet; concrete implementations land in subsequent lifts
(B/C/D) which move the existing ``backend/gateway`` + ``backend/routing`` +
``backend/executors`` code behind this facade.

Design source: ``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md`` v8 §5.1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LlmRoutingHints:
    """Caller-visible hints — 라우팅 룰의 *입력* 이지 결정이 아님."""

    pipeline: str | None = None
    workspace_id: uuid.UUID | None = None


@dataclass(frozen=True)
class LlmRequest:
    workspace_id: uuid.UUID
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    hints: LlmRoutingHints = field(default_factory=LlmRoutingHints)


@dataclass(frozen=True)
class LlmResult:
    content: str
    usage_prompt_tokens: int
    usage_completion_tokens: int
    tool_calls: tuple[dict[str, Any], ...] = ()
    resolved_model_label: str = ""


@runtime_checkable
class Router(Protocol):
    async def invoke(self, request: LlmRequest) -> LlmResult: ...
