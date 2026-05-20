"""SkillContext + protocols for plugin execution.

This is the data carrier passed to every ``@p.inbound`` / ``@p.outbound``
/ ``@p.action`` call. The :class:`SkillContext` defined here is the
*framework* shape — knowledge / vault coupling lives in
``backend.knowledge`` and is plugged in as a Protocol-typed attribute
when that bundle lands.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

ToolHandler = Callable[[str, str, dict[str, Any]], Awaitable[str]]


@runtime_checkable
class LLMClient(Protocol):
    async def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_handler: ToolHandler | None = None,
        max_rounds: int = 10,
    ) -> str: ...


@runtime_checkable
class ChatInterface(Protocol):
    async def chat(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
        context_paths: list[str] | None = None,
    ) -> str: ...


@runtime_checkable
class RetrieverInterface(Protocol):
    async def search(
        self,
        query: str,
        context_dirs: list[str] | None = None,
        max_chars: int = 50_000,
        top_k: int = 20,
    ) -> str: ...


@runtime_checkable
class NotificationInterface(Protocol):
    async def send(self, message: str) -> None: ...


@runtime_checkable
class KnowledgeBackend(Protocol):
    """Placeholder for the BSage-style vault backend.

    Concrete implementation lands with ``backend/knowledge/`` lift; until
    then this is just a name to type-check against.
    """

    async def write_seed(self, source: str, data: dict[str, Any]) -> str: ...


@dataclass
class SkillContext:
    """Container injected into every plugin capability call."""

    llm: LLMClient
    config: dict[str, Any]
    logger: structlog.typing.FilteringBoundLogger | Any
    credentials: dict[str, Any] = field(default_factory=dict)
    input_data: dict[str, Any] | None = field(default=None)
    chat: ChatInterface | None = field(default=None)
    retriever: RetrieverInterface | None = field(default=None)
    notify: NotificationInterface | None = field(default=None)
    knowledge: KnowledgeBackend | None = field(default=None)
