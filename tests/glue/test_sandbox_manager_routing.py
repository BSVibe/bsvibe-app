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

from backend.workflow.application.runtime.sandbox_selection import resolve_sandbox_manager
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
    resolved = resolve_sandbox_manager(injected, _Settings(sandbox_enabled=True))
    assert resolved is injected


def test_enabled_builds_docker_manager_not_noop(monkeypatch) -> None:
    captured = DockerSandboxManager(
        docker_host="tcp://x:2375",
        sandbox_image="img",
        idle_reap_seconds=1,
        max_concurrent=1,
    )
    monkeypatch.setattr(
        "backend.workflow.application.runtime.sandbox_selection.get_sandbox_manager",
        lambda: captured,
    )
    resolved = resolve_sandbox_manager(None, _Settings(sandbox_enabled=True))
    assert resolved is captured


def test_disabled_uses_noop_explicitly() -> None:
    resolved = resolve_sandbox_manager(None, _Settings(sandbox_enabled=False))
    assert isinstance(resolved, NoopSandboxManager)


def test_enabled_but_unbuildable_raises_not_silent_host_fallback(monkeypatch) -> None:
    """The anti-regression: enabled + build returns None must NOT degrade to a
    NoopSandboxManager (host execution) — it raises so the failure is loud."""
    monkeypatch.setattr(
        "backend.workflow.application.runtime.sandbox_selection.get_sandbox_manager",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="sandbox_enabled"):
        resolve_sandbox_manager(None, _Settings(sandbox_enabled=True))


# --------------------------------------------------------------------------
# The other half: an UNSET flag is not a decision
# --------------------------------------------------------------------------
#
# The resolver above made *enabled-but-unbuildable* loud. It leaves the
# disabled path silent — `settings.sandbox_enabled` false → `NoopSandboxManager()`
# → the agent's shell runs in the worker container. That is correct WHEN THE
# FOUNDER CHOSE IT.
#
# The hazard is that a chosen `false` and an accidental one are indistinguishable.
# Measured 2026-08-26: `backend/config.py` defaults it to False AND
# `deploy/compose.prod.yaml` defaults it to `${BSVIBE_SANDBOX_ENABLED:-false}`.
# So a `.env.prod` that fails to load leaves BOTH layers saying "false", the
# fail-closed guard never fires (it only guards enabled-but-unbuildable), and
# prod silently degrades to host execution. prod today runs `true` — this is
# about the day it doesn't.
#
# The founder's rule is that a setting is not a defect ("사용자 설정은 결함이
# 아니다 … 기준 = 사용자가 다른 값을 골랐나"). So the fix does not override the
# choice — it requires that a choice was MADE.


def _settings(**env: str):
    """Build Settings from an explicit env mapping, ignoring any ambient .env."""
    import os

    from backend.config import Settings

    keep = {k: v for k, v in os.environ.items() if not k.startswith("BSVIBE_")}
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update(env)
    try:
        return Settings(_env_file=None)  # type: ignore[call-arg]
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_prod_with_no_sandbox_decision_refuses_to_start() -> None:
    """An unset flag in prod is not "disabled" — it is "nobody said". Starting
    that way is how a failed `.env.prod` load turns into silent host execution."""
    with pytest.raises(Exception) as exc:
        _settings(BSVIBE_ENVIRONMENT="prod")
    assert "sandbox" in str(exc.value).lower()


def test_prod_may_explicitly_disable_the_sandbox() -> None:
    """POSITIVE CONTROL — the founder's own choice is not a defect. Saying
    ``false`` out loud must keep working; only silence is refused."""
    s = _settings(BSVIBE_ENVIRONMENT="prod", BSVIBE_SANDBOX_ENABLED="false")
    assert s.sandbox_enabled is False


def test_prod_with_the_sandbox_on_is_unaffected() -> None:
    """POSITIVE CONTROL — what prod actually runs today."""
    s = _settings(BSVIBE_ENVIRONMENT="prod", BSVIBE_SANDBOX_ENABLED="true")
    assert s.sandbox_enabled is True


def test_dev_keeps_its_quiet_default() -> None:
    """POSITIVE CONTROL — local dev and the whole test suite build Settings with
    nothing set. Requiring the flag there would brick both."""
    s = _settings()
    assert s.environment == "dev"
    assert s.sandbox_enabled is False
