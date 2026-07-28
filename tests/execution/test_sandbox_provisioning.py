"""Shared sandbox venv provisioning — one source of truth for materializing
a uv worktree's ``.venv`` inside a :class:`SandboxSession`.

Exercises :func:`ensure_sandbox_ready` directly with a scripted box: a uv
project syncs once (with the shared timeout), a non-uv project does not, a
failed / timed-out sync reports not-ready, and a ``SandboxError`` on the
lockfile read (a real sandbox's "no such file") is swallowed to not-ready.
"""

from __future__ import annotations

from dataclasses import dataclass

from structlog.testing import capture_logs

from backend.workflow.application import sandbox_provisioning
from backend.workflow.application.sandbox_provisioning import (
    _UV_SYNC,
    TEST_DB_SETUP_TIMEOUT_S,
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


async def test_empty_lock_logs_no_lockfile_with_empty_reason() -> None:
    """An empty lockfile read → logged with a reason distinguishing it from the
    SandboxError case."""
    box = _ProvisionBox(lock=b"")
    with capture_logs() as logs:
        await ensure_sandbox_ready(box)
    event = next(log for log in logs if log["event"] == "sandbox_venv_no_lockfile")
    assert event["reason"] == "empty"


async def test_missing_lock_read_error_is_not_ready() -> None:
    """A real sandbox raises SandboxError on a missing lockfile → swallowed to
    not-ready, no sync attempted."""
    box = _ProvisionBox(lock_raises=True)
    ready = await ensure_sandbox_ready(box)
    assert ready is False
    assert box.exec_calls == []


async def test_read_error_logs_no_lockfile_with_error_reason() -> None:
    """A SandboxError on the lockfile read → logged with a reason distinct from
    the empty-read case."""
    box = _ProvisionBox(lock_raises=True)
    with capture_logs() as logs:
        await ensure_sandbox_ready(box)
    event = next(log for log in logs if log["event"] == "sandbox_venv_no_lockfile")
    assert event["reason"] == "read_error"


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


async def test_sync_failure_logs_exit_code_and_stderr_tail() -> None:
    """A failed sync logs a warning with the exit code and a stderr tail so the
    failure is diagnosable (not a silent False)."""
    box = _ProvisionBox(
        lock=b"# lockfile",
        sync=SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False),
    )
    with capture_logs() as logs:
        await ensure_sandbox_ready(box)
    event = next(log for log in logs if log["event"] == "sandbox_venv_sync_failed")
    assert event["log_level"] == "warning"
    assert event["exit_code"] == 1
    assert event["timed_out"] is False
    assert "boom" in event["stderr_tail"]


async def test_sync_timeout_reports_not_ready() -> None:
    """A sync that timed out (exit_code None) → not-ready."""
    box = _ProvisionBox(
        lock=b"# lockfile",
        sync=SandboxResult(exit_code=None, stdout="", stderr="", timed_out=True),
    )
    ready = await ensure_sandbox_ready(box)
    assert ready is False


async def test_sync_timeout_logs_timed_out() -> None:
    """A timed-out sync logs the warning with ``timed_out=True``."""
    box = _ProvisionBox(
        lock=b"# lockfile",
        sync=SandboxResult(exit_code=None, stdout="", stderr="", timed_out=True),
    )
    with capture_logs() as logs:
        await ensure_sandbox_ready(box)
    event = next(log for log in logs if log["event"] == "sandbox_venv_sync_failed")
    assert event["timed_out"] is True


async def test_successful_sync_is_logged() -> None:
    """A healthy sync emits ``sandbox_venv_synced`` so a good pre-warm is
    visible in the logs, not only failures."""
    box = _ProvisionBox(lock=b"# lockfile")
    with capture_logs() as logs:
        await ensure_sandbox_ready(box)
    assert any(log["event"] == "sandbox_venv_synced" for log in logs)


async def test_idempotent_warm_resync_still_ready() -> None:
    """A warm re-sync (near-noop, exit 0) still reports ready — the function is
    safe to call per-acquire on a container that persists across runs."""
    box = _ProvisionBox(lock=b"# lockfile")
    assert await ensure_sandbox_ready(box) is True
    assert await ensure_sandbox_ready(box) is True
    assert box.exec_calls == [(_UV_SYNC, VENV_SYNC_TIMEOUT_S)] * 2


# --- Optional test-DB setup command (sandbox_test_db_setup_cmd) -------------


@dataclass
class _FakeSettings:
    sandbox_test_db_setup_cmd: str = ""


class _MultiCmdBox:
    """SandboxSession double returning per-command exec results (uv.lock present)."""

    def __init__(self, results: dict[str, SandboxResult]) -> None:
        self._results = results
        self.exec_calls: list[tuple[str, float]] = []

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.exec_calls.append((command, timeout_s))
        return self._results.get(
            command, SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return b"# lockfile"

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        return None

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return []


def _patch_setup_cmd(monkeypatch, cmd: str) -> None:
    monkeypatch.setattr(
        sandbox_provisioning, "get_settings", lambda: _FakeSettings(sandbox_test_db_setup_cmd=cmd)
    )


async def test_setup_cmd_runs_after_venv_sync(monkeypatch) -> None:
    """A non-empty ``sandbox_test_db_setup_cmd`` runs in the box after uv sync."""
    _patch_setup_cmd(monkeypatch, "uv run alembic upgrade head")
    box = _MultiCmdBox({})
    ready = await ensure_sandbox_ready(box)
    assert ready is True
    assert box.exec_calls == [
        (_UV_SYNC, VENV_SYNC_TIMEOUT_S),
        ("uv run alembic upgrade head", TEST_DB_SETUP_TIMEOUT_S),
    ]


async def test_empty_setup_cmd_does_not_run(monkeypatch) -> None:
    """Empty setup cmd (the default) → only the venv sync runs."""
    _patch_setup_cmd(monkeypatch, "")
    box = _MultiCmdBox({})
    ready = await ensure_sandbox_ready(box)
    assert ready is True
    assert box.exec_calls == [(_UV_SYNC, VENV_SYNC_TIMEOUT_S)]


async def test_setup_cmd_failure_does_not_change_venv_readiness(monkeypatch) -> None:
    """The core regression guard: venv-readiness is decoupled from the DB setup.

    A setup_cmd (DB migration) failure must NOT flip the return value — the venv
    synced fine, so the box IS ready. The failure is logged, not returned."""
    _patch_setup_cmd(monkeypatch, "uv run alembic upgrade head")
    box = _MultiCmdBox(
        {
            "uv run alembic upgrade head": SandboxResult(
                exit_code=1, stdout="", stderr="migration boom", timed_out=False
            )
        }
    )
    with capture_logs() as logs:
        ready = await ensure_sandbox_ready(box)
    assert ready is True  # venv is ready even though the migration failed
    assert box.exec_calls == [
        (_UV_SYNC, VENV_SYNC_TIMEOUT_S),
        ("uv run alembic upgrade head", TEST_DB_SETUP_TIMEOUT_S),
    ]
    event = next(log for log in logs if log["event"] == "sandbox_test_db_setup_failed")
    assert event["log_level"] == "warning"
    assert event["exit_code"] == 1


async def test_setup_cmd_success_is_logged(monkeypatch) -> None:
    """A successful setup_cmd emits ``sandbox_test_db_setup_ok``."""
    _patch_setup_cmd(monkeypatch, "uv run alembic upgrade head")
    box = _MultiCmdBox({})
    with capture_logs() as logs:
        ready = await ensure_sandbox_ready(box)
    assert ready is True
    assert any(log["event"] == "sandbox_test_db_setup_ok" for log in logs)


async def test_setup_cmd_stderr_password_is_scrubbed(monkeypatch) -> None:
    """A DB URL with a password in the setup stderr is masked before logging —
    the raw password must never reach the logs."""
    _patch_setup_cmd(monkeypatch, "uv run alembic upgrade head")
    box = _MultiCmdBox(
        {
            "uv run alembic upgrade head": SandboxResult(
                exit_code=1,
                stdout="",
                stderr="could not connect: postgresql+asyncpg://bsvibe:secret@host:5432/db",
                timed_out=False,
            )
        }
    )
    with capture_logs() as logs:
        await ensure_sandbox_ready(box)
    event = next(log for log in logs if log["event"] == "sandbox_test_db_setup_failed")
    assert "secret" not in event["stderr_tail"]
    assert "://***@" in event["stderr_tail"]
