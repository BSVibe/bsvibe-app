"""#691 — a box that carries a product's declared secrets to every command it runs.

The client box already did this (``ClientWorkerSandboxManager(env=...)``). The server box is
process-wide and shared by every run, so the secrets cannot live on it: they ride on a thin
per-run wrapper instead, and the box underneath stays exactly as it was.

The caller's own ``env`` wins over the declared one — the verification stack passes its boot
command exactly what it means to boot with, and a product default must not override that.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from backend.workflow.infrastructure.sandbox.protocol import (
    SandboxManager,
    SandboxResult,
    SandboxSession,
)


class DeclaredEnvSession:
    """A :class:`SandboxSession` whose commands all get ``env``.

    Everything else is the inner session's — including the traits the loop reads off a box by
    ``getattr`` (``runs_in_place``, ``provisions_venv``), which a wrapper must not hide.
    """

    def __init__(self, inner: SandboxSession, env: Mapping[str, str]) -> None:
        self._inner = inner
        self._env = dict(env)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def workspace_mount(self) -> str:
        return self._inner.workspace_mount

    async def exec(
        self,
        command: str,
        *,
        timeout_s: float,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        return await self._inner.exec(
            command, timeout_s=timeout_s, shell=shell, env={**self._env, **(env or {})}
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return await self._inner.read_file(rel_path, max_bytes)

    async def write_file(self, rel_path: str, content: bytes) -> None:
        await self._inner.write_file(rel_path, content)

    async def list_dir(self, rel_path: str) -> list[str]:
        return await self._inner.list_dir(rel_path)


class DeclaredEnvManager:
    """A :class:`SandboxManager` whose sessions are :class:`DeclaredEnvSession`."""

    def __init__(self, inner: SandboxManager, env: Mapping[str, str]) -> None:
        self._inner = inner
        self._env = dict(env)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> SandboxSession:
        return DeclaredEnvSession(await self._inner.acquire(project_id, workspace_path), self._env)

    async def release(self, project_id: uuid.UUID) -> None:
        await self._inner.release(project_id)

    async def reap_idle(self) -> None:
        await self._inner.reap_idle()

    async def health(self) -> bool:
        return await self._inner.health()


def with_declared_env(manager: SandboxManager, env: Mapping[str, str] | None) -> SandboxManager:
    """``manager`` carrying ``env`` — or ``manager`` itself when there is nothing to carry."""
    return DeclaredEnvManager(manager, env) if env else manager


def with_session_env(session: SandboxSession, env: Mapping[str, str] | None) -> SandboxSession:
    """``session`` carrying ``env`` — or ``session`` itself when there is nothing to carry."""
    return DeclaredEnvSession(session, env) if env else session


__all__ = ["DeclaredEnvManager", "DeclaredEnvSession", "with_declared_env", "with_session_env"]
