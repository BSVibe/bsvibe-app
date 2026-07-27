"""shell_exec timeout is CONFIGURABLE, not a hardcoded 30s.

Root cause of the "30-minute flail": ``ToolRegistry._shell_exec`` hard-killed
EVERY sandbox command at ``SHELL_TIMEOUT_S = 30.0``s, so ``uv run pytest`` /
``uv sync`` on a real repo was killed at 30s and the agent retried inside its
turn until the whole-turn cap. The timeout now comes from
``settings.shell_exec_timeout_s`` (default 900s), with an OPTIONAL per-call
``timeout_s`` bounded by ``settings.shell_exec_timeout_max_s`` so a big suite
can request longer while a runaway cannot request infinity.

These drive the REAL ``ToolRegistry._shell_exec`` (via ``invoke``) with a
recording stub sandbox that captures the ``timeout_s`` each command is run
with — so the assertion is on the value actually handed to the sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import get_settings
from backend.workflow.infrastructure.sandbox import SandboxResult
from backend.workflow.infrastructure.tools import ToolError, ToolRegistry


class _RecordingSandbox:
    """Stub SandboxSession recording every ``(command, timeout_s)`` pair."""

    def __init__(self, *, timed_out: bool = False) -> None:
        self.calls: list[tuple[str, float]] = []
        self._timed_out = timed_out

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.calls.append((command, timeout_s))
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=self._timed_out)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:  # pragma: no cover
        return b""

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        return None

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return []


def _registry(tmp_path: Path, sandbox: _RecordingSandbox) -> ToolRegistry:
    return ToolRegistry(workspace_dir=tmp_path, sandbox=sandbox)


async def test_shell_exec_uses_configured_default_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_s", 777.0)
    sandbox = _RecordingSandbox()
    registry = _registry(tmp_path, sandbox)

    result = await registry.invoke("shell_exec", {"command": "echo hi"})

    assert result.startswith("exit=0"), result
    assert sandbox.calls == [("echo hi", 777.0)]


async def test_shell_exec_is_not_hardcoded_to_30s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a long test-suite command must get the raised default (900s),
    never the old 30s that killed it mid-run."""
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_s", 900.0)
    sandbox = _RecordingSandbox()
    registry = _registry(tmp_path, sandbox)

    await registry.invoke("shell_exec", {"command": "uv run pytest"})

    _command, timeout = sandbox.calls[0]
    assert timeout == 900.0
    assert timeout != 30.0


async def test_per_call_timeout_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_s", 100.0)
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_max_s", 3600.0)
    sandbox = _RecordingSandbox()
    registry = _registry(tmp_path, sandbox)

    await registry.invoke("shell_exec", {"command": "uv run pytest", "timeout_s": 1200})

    assert sandbox.calls[0][1] == 1200.0


async def test_per_call_timeout_clamped_to_hard_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-call request ABOVE the hard max is clamped — a runaway cannot
    request infinity."""
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_s", 100.0)
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_max_s", 3600.0)
    sandbox = _RecordingSandbox()
    registry = _registry(tmp_path, sandbox)

    await registry.invoke("shell_exec", {"command": "uv run pytest", "timeout_s": 999999})

    assert sandbox.calls[0][1] == 3600.0


async def test_per_call_non_positive_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_s", 88.0)
    sandbox = _RecordingSandbox()
    registry = _registry(tmp_path, sandbox)

    await registry.invoke("shell_exec", {"command": "echo hi", "timeout_s": 0})

    assert sandbox.calls[0][1] == 88.0


async def test_timeout_error_reports_configured_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "shell_exec_timeout_s", 555.0)
    sandbox = _RecordingSandbox(timed_out=True)
    registry = _registry(tmp_path, sandbox)

    with pytest.raises(ToolError) as excinfo:
        await registry.invoke("shell_exec", {"command": "sleep 999"})

    assert "555" in str(excinfo.value)
    assert "timed out" in str(excinfo.value).lower()
