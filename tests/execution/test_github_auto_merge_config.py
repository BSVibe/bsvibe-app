"""PR2 — the GitHub CI-green auto-merge opt-in flag defaults OFF.

Additive setting for a later ``MergeWatchWorker``; nothing consumes it
yet. OFF keeps the existing "open PR, human merges" behavior.
"""

from __future__ import annotations

import pytest

from backend.config import Settings


def test_github_auto_merge_defaults_off() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.github_auto_merge_enabled is False


def test_github_auto_merge_enable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BSVIBE_GITHUB_AUTO_MERGE_ENABLED", "true")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.github_auto_merge_enabled is True
