"""Tests for worker capability detection + executor selection (Lift 3).

``detect_capabilities`` probes ``shutil.which`` for the CLI binaries; only
``claude_code`` has a real executor in this lift (codex / opencode may be
*detected* but their executors are a follow-up). ``select_executor`` maps a
capability/executor_type string to an :class:`ExecutorProtocol` instance.
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from backend.executors.worker import executors as exmod
from backend.executors.worker.claude_code import ClaudeCodeExecutor
from backend.executors.worker.executors import (
    ExecutorProtocol,
    detect_capabilities,
    select_executor,
)


def _patch_which(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
    def _which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in present else None

    monkeypatch.setattr(shutil, "which", _which)


def test_detect_claude_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"claude"})
    assert detect_capabilities() == ["claude_code"]


def test_detect_none_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, set())
    assert detect_capabilities() == []


def test_detect_lists_codex_opencode_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # codex / opencode are detectable here (a follow-up lift adds their
    # executors); claude_code stays first.
    _patch_which(monkeypatch, {"claude", "codex", "opencode"})
    caps = detect_capabilities()
    assert caps[0] == "claude_code"
    assert set(caps) == {"claude_code", "codex", "opencode"}


def test_select_claude_code_returns_executor() -> None:
    executor = select_executor("claude_code")
    assert isinstance(executor, ClaudeCodeExecutor)
    assert isinstance(executor, ExecutorProtocol)


def test_select_unknown_raises() -> None:
    with pytest.raises(ValueError, match="codex"):
        select_executor("codex")
    with pytest.raises(ValueError):
        select_executor("nope")


def test_protocol_runtime_checkable() -> None:
    # A bare object is NOT an ExecutorProtocol.
    assert not isinstance(object(), ExecutorProtocol)


async def test_collect_marks_failure_on_error_chunk() -> None:
    async def _stream() -> Any:
        yield exmod.ExecutionChunk(delta="partial")
        yield exmod.ExecutionChunk(done=True, error="kaboom")

    result = await exmod.collect(_stream())
    assert result.success is False
    assert result.stdout == "partial"
    assert result.error_message == "kaboom"
