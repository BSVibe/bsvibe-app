"""Sandbox-manager routing is EXPLICIT — no silent host fallback.

[[bsvibe-no-implicit-routing]]: BSVibe never silently substitutes a degraded
backend. The prior `sandbox_manager or build_sandbox_manager() or
NoopSandboxManager()` chain hid a real failure mode — when the operator
INTENDED a sandbox (`sandbox_enabled=true`) but none could be built, the loop
silently ran the verifier's `command` checks as host subprocesses in the worker
container (no toolchain → the confusing `/app/.venv/bin/python: No module named
pytest`). The resolver now branches explicitly: an enabled-but-unbuildable
sandbox RAISES rather than degrading to host execution.
"""

from __future__ import annotations

import pytest

from backend.workflow.application.runtime.agent_runtime import _resolve_sandbox_manager
from backend.workflow.infrastructure.sandbox import (
    DockerSandboxManager,
    NoopSandboxManager,
)


class _Settings:
    def __init__(self, *, sandbox_enabled: bool) -> None:
        self.sandbox_enabled = sandbox_enabled
        self.docker_host = "tcp://sandbox-dind:2375"
        self.sandbox_image = "bsvibe-sandbox:latest"
        self.sandbox_idle_reap_seconds = 1800
        self.sandbox_max_concurrent = 2
        self.sandbox_user = "0:0"


def test_explicit_manager_is_used_as_is() -> None:
    injected = NoopSandboxManager()
    resolved = _resolve_sandbox_manager(injected, _Settings(sandbox_enabled=True))
    assert resolved is injected


def test_enabled_builds_docker_manager_not_noop(monkeypatch) -> None:
    captured = DockerSandboxManager(
        docker_host="tcp://x:2375",
        sandbox_image="img",
        idle_reap_seconds=1,
        max_concurrent=1,
    )
    monkeypatch.setattr(
        "backend.workflow.application.runtime.agent_runtime.build_sandbox_manager",
        lambda: captured,
    )
    resolved = _resolve_sandbox_manager(None, _Settings(sandbox_enabled=True))
    assert resolved is captured


def test_disabled_uses_noop_explicitly() -> None:
    resolved = _resolve_sandbox_manager(None, _Settings(sandbox_enabled=False))
    assert isinstance(resolved, NoopSandboxManager)


def test_enabled_but_unbuildable_raises_not_silent_host_fallback(monkeypatch) -> None:
    """The anti-regression: enabled + build returns None must NOT degrade to a
    NoopSandboxManager (host execution) — it raises so the failure is loud."""
    monkeypatch.setattr(
        "backend.workflow.application.runtime.agent_runtime.build_sandbox_manager",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="sandbox_enabled"):
        _resolve_sandbox_manager(None, _Settings(sandbox_enabled=True))
