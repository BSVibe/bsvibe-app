"""Process-wide sandbox manager resolver.

``sandbox_enabled`` false ⇒ no manager (``None``). Callers that get
``None`` pass ``sandbox_session=None`` downstream so the original host
paths run unchanged. Lifted from BSNexus.
"""

from __future__ import annotations

from backend.config import get_settings
from backend.supervisor.sandbox.docker_manager import DockerSandboxManager
from backend.supervisor.sandbox.protocol import SandboxManager

_manager: SandboxManager | None = None
_resolved = False


def build_sandbox_manager() -> SandboxManager | None:
    settings = get_settings()
    if settings.sandbox_enabled:
        return DockerSandboxManager(
            docker_host=settings.docker_host,
            sandbox_image=settings.sandbox_image,
            idle_reap_seconds=settings.sandbox_idle_reap_seconds,
            max_concurrent=settings.sandbox_max_concurrent,
        )
    return None


def get_sandbox_manager() -> SandboxManager | None:
    global _manager, _resolved  # noqa: PLW0603 — module-level singleton
    if not _resolved:
        _manager = build_sandbox_manager()
        _resolved = True
    return _manager


def reset_sandbox_manager() -> None:
    global _manager, _resolved  # noqa: PLW0603 — module-level singleton
    _manager = None
    _resolved = False
