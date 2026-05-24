"""Tests for worker capability detection + executor selection.

``detect_capabilities`` probes ``shutil.which`` for the CLI binaries.
``select_executor`` maps a capability/executor_type string to an
:class:`ExecutorProtocol` instance (``claude_code`` / ``codex`` / ``opencode``).
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from backend.executors.worker import executors as exmod
from backend.executors.worker.claude_code import ClaudeCodeExecutor
from backend.executors.worker.codex import CodexExecutor
from backend.executors.worker.executors import (
    ExecutorProtocol,
    detect_capabilities,
    sanitized_subprocess_env,
    select_executor,
)
from backend.executors.worker.opencode import OpenCodeExecutor


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
    # claude_code stays first; codex / opencode are detected when on PATH.
    _patch_which(monkeypatch, {"claude", "codex", "opencode"})
    caps = detect_capabilities()
    assert caps[0] == "claude_code"
    assert set(caps) == {"claude_code", "codex", "opencode"}


def test_detect_codex_opencode_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"codex", "opencode"})
    assert set(detect_capabilities()) == {"codex", "opencode"}


def test_select_claude_code_returns_executor() -> None:
    executor = select_executor("claude_code")
    assert isinstance(executor, ClaudeCodeExecutor)
    assert isinstance(executor, ExecutorProtocol)


def test_select_codex_returns_executor() -> None:
    executor = select_executor("codex")
    assert isinstance(executor, CodexExecutor)
    assert isinstance(executor, ExecutorProtocol)


def test_select_opencode_returns_executor() -> None:
    executor = select_executor("opencode")
    assert isinstance(executor, OpenCodeExecutor)
    assert isinstance(executor, ExecutorProtocol)


def test_select_unknown_raises() -> None:
    with pytest.raises(ValueError, match="nope"):
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


# ── Sanitized subprocess env (parent Claude-Code session leakage) ─────────────


def test_sanitized_env_strips_claude_code_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any key starting with ``CLAUDE_CODE_`` is a parent-session/agent marker
    # that confuses a freshly spawned ``claude`` CLI — strip the whole prefix.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc123")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_ANYTHING_ELSE", "x")

    env = sanitized_subprocess_env()

    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CLAUDE_CODE_ANYTHING_ELSE" not in env


def test_sanitized_env_strips_named_session_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "0.1.0")
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    monkeypatch.setenv("AI_AGENT", "claude-code")

    env = sanitized_subprocess_env()

    assert "CLAUDECODE" not in env
    assert "CLAUDE_AGENT_SDK_VERSION" not in env
    assert "CLAUDE_EFFORT" not in env
    assert "AI_AGENT" not in env


def test_sanitized_env_retains_normal_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # Normal env — PATH/HOME, ANTHROPIC_* auth, and even a non-session
    # ``CLAUDE_`` key like ``CLAUDE_CONFIG_DIR`` — must be preserved.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/worker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/worker/.claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak")

    env = sanitized_subprocess_env()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/worker"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/worker/.claude"
    assert "CLAUDE_CODE_SESSION_ID" not in env


def test_sanitized_env_accepts_explicit_base() -> None:
    # The helper can sanitize a provided base mapping (used by opencode, which
    # layers its own OPENCODE_CONFIG_CONTENT before spawning).
    base = {
        "PATH": "/bin",
        "CLAUDE_CODE_SESSION_ID": "leak",
        "CLAUDECODE": "1",
        "OPENCODE_CONFIG_CONTENT": "{}",
    }
    env = sanitized_subprocess_env(base)

    assert env["PATH"] == "/bin"
    assert env["OPENCODE_CONFIG_CONTENT"] == "{}"
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDECODE" not in env
