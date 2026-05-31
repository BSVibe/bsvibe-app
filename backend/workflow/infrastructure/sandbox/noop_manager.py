"""Host-side sandbox backend — the fallback when ``sandbox_enabled`` is false.

Runs commands as host subprocesses and file ops as host filesystem IO,
rooted at the project workspace directory. Lifted from BSNexus.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from pathlib import Path

from backend.workflow.infrastructure.sandbox.errors import SandboxError
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult


class NoopSandboxSession:
    """A host-side session rooted at a workspace directory."""

    def __init__(self, workspace_path: str) -> None:
        self._root = Path(workspace_path).resolve()

    @property
    def workspace_mount(self) -> str:
        return str(self._root)

    def _resolve(self, rel_path: str) -> Path:
        candidate = (self._root / rel_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise SandboxError(f"path {rel_path!r} escapes the workspace") from exc
        return candidate

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        if shell:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self._root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            try:
                parts = shlex.split(command)
            except ValueError as exc:
                raise SandboxError(f"bad shell syntax: {exc}") from exc
            if not parts:
                raise SandboxError("empty command")
            try:
                process = await asyncio.create_subprocess_exec(
                    *parts,
                    cwd=str(self._root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                return SandboxResult(
                    exit_code=127,
                    stdout="",
                    stderr=f"command not found: {parts[0]}",
                    timed_out=False,
                )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return SandboxResult(
                exit_code=None,
                stdout="",
                stderr=f"timed out after {timeout_s}s",
                timed_out=True,
            )
        return SandboxResult(
            exit_code=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            timed_out=False,
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        target = self._resolve(rel_path)
        return await asyncio.to_thread(self._read_capped, target, max_bytes)

    async def write_file(self, rel_path: str, content: bytes) -> None:
        target = self._resolve(rel_path)
        await asyncio.to_thread(self._write, target, content)

    async def list_dir(self, rel_path: str) -> list[str]:
        target = self._resolve(rel_path)
        return await asyncio.to_thread(self._list, target)

    @staticmethod
    def _read_capped(path: Path, cap: int) -> bytes:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SandboxError(f"read_file: {exc}") from exc
        return data[:cap]

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError as exc:
            raise SandboxError(f"write_file: {exc}") from exc

    @staticmethod
    def _list(path: Path) -> list[str]:
        if not path.is_dir():
            raise SandboxError(f"list_dir: not a directory: {path}")
        return sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())


class NoopSandboxManager:
    """Host-side manager — every project shares the host."""

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> NoopSandboxSession:
        return NoopSandboxSession(workspace_path)

    async def release(self, project_id: uuid.UUID) -> None:
        return None

    async def reap_idle(self) -> None:
        return None

    async def health(self) -> bool:
        return True
