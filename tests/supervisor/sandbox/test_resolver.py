"""Tests for the sandbox manager resolver singleton."""

from __future__ import annotations

import pytest

from backend.supervisor.sandbox import (
    DockerSandboxManager,
    build_sandbox_manager,
    get_sandbox_manager,
    reset_sandbox_manager,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Drop any cached config + singleton between tests.
    from backend.config import get_settings

    get_settings.cache_clear()
    reset_sandbox_manager()
    yield
    get_settings.cache_clear()
    reset_sandbox_manager()


class TestBuild:
    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "false")
        assert build_sandbox_manager() is None

    def test_returns_docker_manager_when_enabled(self, monkeypatch):
        monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "true")
        monkeypatch.setenv("BSVIBE_SANDBOX_IMAGE", "bsvibe-sandbox:test")
        monkeypatch.setenv("BSVIBE_DOCKER_HOST", "tcp://sb:2375")
        mgr = build_sandbox_manager()
        assert isinstance(mgr, DockerSandboxManager)


class TestSingleton:
    def test_caches_first_resolution(self, monkeypatch):
        monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "true")
        m1 = get_sandbox_manager()
        m2 = get_sandbox_manager()
        assert m1 is m2

    def test_reset_then_get_returns_new_object(self, monkeypatch):
        monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "true")
        m1 = get_sandbox_manager()
        reset_sandbox_manager()
        m2 = get_sandbox_manager()
        assert m1 is not m2

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "false")
        assert get_sandbox_manager() is None
