"""Sandbox protocol — the seam between the work loop / verifier and
the execution backend.

``SandboxManager`` owns per-project sandbox lifecycle; ``SandboxSession``
is a handle to one project's running sandbox. Everything that needs to
run a command or touch a file depends on these Protocols — never on
``docker`` directly. The docker shell-out lives in exactly one
implementation (``DockerSandboxManager``); ``NoopSandboxManager`` is the
host-side fallback.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of one command run inside a sandbox session.

    ``exit_code`` is ``None`` when the command was killed before it
    could exit (timeout). ``timed_out`` disambiguates that case.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


@runtime_checkable
class SandboxSession(Protocol):
    """A handle to one project's running sandbox."""

    @property
    def workspace_mount(self) -> str: ...

    async def exec(
        self, command: str, *, timeout_s: float, shell: bool = False
    ) -> SandboxResult: ...

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes: ...

    async def write_file(self, rel_path: str, content: bytes) -> None: ...

    async def list_dir(self, rel_path: str) -> list[str]: ...


@runtime_checkable
class SandboxManager(Protocol):
    """Per-project sandbox lifecycle. One sandbox per project, created
    lazily on first work dispatch, reused across runs, reaped on idle."""

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> SandboxSession: ...

    async def release(self, project_id: uuid.UUID) -> None: ...

    async def reap_idle(self) -> None: ...

    async def health(self) -> bool: ...
