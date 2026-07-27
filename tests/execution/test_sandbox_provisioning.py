"""Shared sandbox venv provisioning — one source of truth for materializing
a uv worktree's ``.venv`` inside a :class:`SandboxSession`.

Exercises :func:`ensure_sandbox_ready` directly with a scripted box: a uv
project syncs once (with the shared timeout), a non-uv project does not, a
failed / timed-out sync reports not-ready, and a ``SandboxError`` on the
lockfile read (a real sandbox's "no such file") is swallowed to not-ready.
"""

from __future__ import annotations

from backend.workflow.application.sandbox_provisioning import (
    _UV_SYNC,
    VENV_SYNC_TIMEOUT_S,
    ensure_sandbox_ready,
)
from backend.workflow.infrastructure.sandbox import SandboxError
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult


class _ProvisionBox:
    """A scripted SandboxSession recording ``(command, timeout_s)`` pairs.

    ``lock`` sets the ``uv.lock`` read result; ``lock_raises`` makes the read
    raise :class:`SandboxError` (a real sandbox's missing-file). ``sync``
    scripts the ``uv sync`` result."""

    def __init__(
        self,
        *,
        lock: bytes = b"",
        lock_raises: bool = False,
        sync: SandboxResult | None = None,
    ) -> None:
        self._lock = lock
        self._lock_raises = lock_raises
        self._sync = sync or SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)
        self.exec_calls: list[tuple[str, float]] = []
        self.read_calls: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.exec_calls.append((command, timeout_s))
        return self._sync

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        self.read_calls.append(rel_path)
        if self._lock_raises:
            raise SandboxError(f"no such file: {rel_path}")
        return self._lock

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        return None

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return []


async def test_uv_project_syncs_once_with_shared_timeout() -> None:
    """uv.lock present → ``uv sync --frozen --all-extras`` runs once with the
    shared (non-short) timeout and reports ready."""
    box = _ProvisionBox(lock=b"# lockfile")
    ready = await ensure_sandbox_ready(box)
    assert ready is True
    assert "uv.lock" in box.read_calls  # detected via the lockfile read
    assert box.exec_calls == [(_UV_SYNC, VENV_SYNC_TIMEOUT_S)]


async def test_non_uv_project_does_not_sync() -> None:
    """No uv.lock → not a uv project → no sync, reports not-ready."""
    box = _ProvisionBox(lock=b"")
    ready = await ensure_sandbox_ready(box)
    assert ready is False
    assert box.exec_calls == []


async def test_missing_lock_read_error_is_not_ready() -> None:
    """A real sandbox raises SandboxError on a missing lockfile → swallowed to
    not-ready, no sync attempted."""
    box = _ProvisionBox(lock_raises=True)
    ready = await ensure_sandbox_ready(box)
    assert ready is False
    assert box.exec_calls == []


async def test_sync_failure_reports_not_ready() -> None:
    """uv.lock present but sync exits non-zero → not-ready (caller runs bare,
    fails honestly rather than against a half-built venv)."""
    box = _ProvisionBox(
        lock=b"# lockfile",
        sync=SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False),
    )
    ready = await ensure_sandbox_ready(box)
    assert ready is False
    assert box.exec_calls == [(_UV_SYNC, VENV_SYNC_TIMEOUT_S)]  # attempted


async def test_sync_timeout_reports_not_ready() -> None:
    """A sync that timed out (exit_code None) → not-ready."""
    box = _ProvisionBox(
        lock=b"# lockfile",
        sync=SandboxResult(exit_code=None, stdout="", stderr="", timed_out=True),
    )
    ready = await ensure_sandbox_ready(box)
    assert ready is False


async def test_idempotent_warm_resync_still_ready() -> None:
    """A warm re-sync (near-noop, exit 0) still reports ready — the function is
    safe to call per-acquire on a container that persists across runs."""
    box = _ProvisionBox(lock=b"# lockfile")
    assert await ensure_sandbox_ready(box) is True
    assert await ensure_sandbox_ready(box) is True
    assert box.exec_calls == [(_UV_SYNC, VENV_SYNC_TIMEOUT_S)] * 2
