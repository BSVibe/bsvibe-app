"""Shared sandbox venv provisioning — one source of truth.

Materializes a uv-managed worktree's ``.venv`` (incl. extras — pytest/ruff
live there) inside a :class:`SandboxSession` so any command run in the box
resolves the project's full dependency tree.

This is called from BOTH ends of the sandbox's life:

* :mod:`backend.workflow.application.agent_loop` — ONCE at sandbox acquire,
  so the INLINE ``shell_exec`` path (the agent running ``uv run pytest``
  mid-turn) sees a ready ``.venv`` without first having to ``uv sync`` by
  hand.
* :class:`~backend.workflow.application.verification_service.VerificationService`
  — before it runs command checks / derived-gate / demonstration probes.

Idempotent by design: ``uv sync --frozen`` is a near-noop once ``.venv``
already exists, so calling it per-acquire on a container that persists
across runs is cheap. Detection is by ``uv.lock`` presence (a *read*, not
an exec — a missing lock raises :class:`SandboxError` on a real sandbox and
returns empty on the host double, so a non-uv worktree never triggers a
sync). Best-effort: a sync failure returns ``False`` so a caller runs the
command bare and it fails HONESTLY rather than passing against a half-built
environment.
"""

from __future__ import annotations

import structlog

from backend.config import get_settings
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxSession

logger = structlog.get_logger(__name__)

# The one sync command + its timeout, shared by acquire and verify. ``uv sync
# --frozen`` respects the lockfile exactly (no resolution); ``--all-extras``
# pulls the dev/test group so pytest + ruff resolve. The 600s ceiling covers a
# cold first sync (a warm re-sync is a near-noop); it is NOT a short timeout —
# do not lower it below a real dependency build.
_UV_SYNC = "uv sync --frozen --all-extras"
VENV_SYNC_TIMEOUT_S = 600.0
# Ceiling for the optional test-DB setup command (``sandbox_test_db_setup_cmd``,
# e.g. ``uv run alembic upgrade head``). Same scale as the venv sync — a real
# migration chain against a fresh PG is not a short op.
TEST_DB_SETUP_TIMEOUT_S = 600.0


async def ensure_sandbox_ready(box: SandboxSession) -> bool:
    """Materialize ``/work/.venv`` for a uv worktree and report readiness.

    Returns ``True`` when the box is a uv project and the sync succeeded (the
    caller may then prepend ``{workspace_mount}/.venv/bin`` to ``PATH``);
    ``False`` for a non-uv worktree (no ``uv.lock``) or a sync that failed /
    timed out. Never raises for the ordinary "no lockfile" case.
    """
    try:
        lock = await box.read_file("uv.lock", 64)
    except SandboxError:
        return False
    if not lock:
        return False
    sync = await box.exec(_UV_SYNC, timeout_s=VENV_SYNC_TIMEOUT_S, shell=True)
    if sync.exit_code != 0 or sync.timed_out:
        return False
    # Optional test-DB provisioning (e.g. ``uv run alembic upgrade head``) after
    # the venv is ready — the injected BSVIBE_MIGRATION_DATABASE_URL is visible
    # to ``docker exec`` since it lives on the sandbox container. Best-effort +
    # logged: a setup failure returns degraded (False) rather than crashing the
    # run — same honesty contract as the venv sync.
    setup_cmd = get_settings().sandbox_test_db_setup_cmd
    if setup_cmd:
        setup = await box.exec(setup_cmd, timeout_s=TEST_DB_SETUP_TIMEOUT_S, shell=True)
        if setup.exit_code != 0 or setup.timed_out:
            logger.warning(
                "sandbox_test_db_setup_failed",
                exit_code=setup.exit_code,
                timed_out=setup.timed_out,
            )
            return False
    return True
