"""SyncManager — extensible vault sync after writes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.core.plugin_loader import PluginMeta
    # TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.core.runtime_config import RuntimeConfig
    PluginMeta = Any
    RuntimeConfig = Any

import structlog

from backend.knowledge._internal.protocols import ContextBuilderLike, RunnerLike

logger = structlog.get_logger(__name__)


class WriteEventType(Enum):
    """Type of vault write operation."""

    SEED = "seed"
    GARDEN = "garden"
    ACTION = "action"


@dataclass
class WriteEvent:
    """Describes a vault write that just occurred."""

    event_type: WriteEventType
    path: Path
    source: str


@runtime_checkable
class SyncBackend(Protocol):
    """Protocol for vault synchronization backends.

    Implementations (e.g. S3SyncBackend, GitSyncBackend) are registered
    with SyncManager and notified after every vault write.
    """

    @property
    def name(self) -> str: ...

    async def sync(self, event: WriteEvent) -> None: ...


class PluginSyncAdapter:
    """Adapts an output Plugin into the SyncBackend protocol.

    Wraps a PluginMeta's execute function so it can be registered
    with SyncManager alongside native SyncBackend implementations.
    """

    def __init__(
        self,
        meta: PluginMeta,
        runner: RunnerLike,
        context_builder: ContextBuilderLike,
    ) -> None:
        self._meta = meta
        self._runner = runner
        self._context_builder = context_builder

    @property
    def name(self) -> str:
        return str(self._meta.name)

    async def sync(self, event: WriteEvent) -> None:
        event_data = {
            "event_type": event.event_type.value,
            "path": str(event.path),
            "source": event.source,
        }
        context = self._context_builder(input_data=event_data)
        await self._runner.run(self._meta, context)


class SyncManager:
    """Manages registered sync backends and dispatches write events.

    Sync failures are logged but never propagated — local writes always
    succeed regardless of sync backend status.
    """

    def __init__(self, *, runtime_config: RuntimeConfig | None = None) -> None:
        self._backends: dict[str, SyncBackend] = {}
        self._output_skills: list[Any] = []
        self._skill_runner: RunnerLike | None = None
        self._context_builder: ContextBuilderLike | None = None
        self._runtime_config = runtime_config

    def register(self, backend: SyncBackend) -> None:
        """Register a sync backend."""
        self._backends[backend.name] = backend
        logger.info("sync_backend_registered", name=backend.name)

    def unregister(self, name: str) -> None:
        """Remove a sync backend by name.

        Raises:
            KeyError: If the backend is not registered.
        """
        del self._backends[name]
        logger.info("sync_backend_unregistered", name=name)

    def list_backends(self) -> list[str]:
        """Return names of all registered backends."""
        return list(self._backends.keys())

    def register_output_skills(
        self,
        skills: list[Any],
        skill_runner: RunnerLike,
        context_builder: ContextBuilderLike,
    ) -> None:
        """Register output skills for execution on write events.

        Replaces any previously registered output skills.

        Args:
            skills: List of output category SkillMeta.
            skill_runner: SkillRunner instance to execute skills.
            context_builder: Callable(input_data=dict) -> SkillContext.
        """
        self._output_skills = []
        self._skill_runner = skill_runner
        self._context_builder = context_builder
        for s in skills:
            if s.category != "output":
                logger.warning(
                    "non_output_skill_rejected",
                    name=s.name,
                    category=s.category,
                )
                continue
            self._output_skills.append(s)
            logger.info("output_skill_registered", name=s.name)

    def register_output_plugins(
        self,
        plugins: list[PluginMeta],
        plugin_runner: RunnerLike,
        context_builder: ContextBuilderLike,
    ) -> None:
        """Register output plugins as SyncBackend adapters.

        Each output-category plugin is wrapped in a PluginSyncAdapter
        and registered as a sync backend.

        Args:
            plugins: List of output category PluginMeta.
            plugin_runner: PluginRunner instance to execute plugins.
            context_builder: Callable(input_data=dict) -> SkillContext.
        """
        for p in plugins:
            if p.category != "output":
                logger.warning(
                    "non_output_plugin_rejected",
                    name=p.name,
                    category=p.category,
                )
                continue
            adapter = PluginSyncAdapter(p, plugin_runner, context_builder)
            self.register(adapter)

    async def notify(self, event: WriteEvent) -> None:
        """Notify all registered backends of a write event.

        Each backend is called independently. Failures are logged
        but never propagated — the local write has already succeeded.
        Only backends/skills present in ``runtime_config.enabled_entries``
        are executed (when a runtime_config is set).
        """
        enabled = self._runtime_config.enabled_entries if self._runtime_config else None

        for name, backend in self._backends.items():
            if enabled is not None and name not in enabled:
                logger.debug("sync_backend_skipped_not_enabled", backend=name)
                continue
            try:
                await backend.sync(event)
                logger.debug("sync_backend_notified", backend=name, path=str(event.path))
            except Exception:
                logger.warning(
                    "sync_backend_failed",
                    backend=name,
                    path=str(event.path),
                    exc_info=True,
                )

        # Execute output skills
        if self._skill_runner and self._context_builder:
            event_data = {
                "event_type": event.event_type.value,
                "path": str(event.path),
                "source": event.source,
            }
            for meta in self._output_skills:
                if enabled is not None and meta.name not in enabled:
                    logger.debug("output_skill_skipped_not_enabled", skill=meta.name)
                    continue
                try:
                    context = self._context_builder(input_data=event_data)
                    await self._skill_runner.run(meta, context)
                    logger.debug("output_skill_executed", skill=meta.name)
                except Exception:
                    logger.warning(
                        "output_skill_failed",
                        skill=meta.name,
                        exc_info=True,
                    )
