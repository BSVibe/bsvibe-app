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

import re

import structlog

from backend.config import get_settings
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxSession

logger = structlog.get_logger(__name__)

# Mask ``://<user>:<pass>@`` credentials in a URL to ``://***@`` before logging —
# alembic/asyncpg stderr routinely echoes the DB URL (password and all). Never
# log a raw password.
_CRED_RE = re.compile(r"://[^/@\s:]+:[^/@\s]+@")


def _scrub(text: str) -> str:
    """Replace URL userinfo credentials with ``***`` so passwords never log."""
    return _CRED_RE.sub("://***@", text)


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
    """Materialize ``/work/.venv`` for a uv worktree and report VENV readiness.

    Returns ``True`` when the box is a uv project and the ``uv sync`` succeeded
    (the caller may then prepend ``{workspace_mount}/.venv/bin`` to ``PATH``);
    ``False`` for a non-uv worktree (no ``uv.lock``) or a sync that failed /
    timed out. Never raises for the ordinary "no lockfile" case.

    The return value reflects VENV-readiness ONLY. The optional test-DB setup
    command (``sandbox_test_db_setup_cmd``) is run for its side effect and
    logged, but its outcome does NOT change the return value: venv-ready and
    db-migration-ready are orthogonal, and a migration failure must not
    misreport a working venv as unusable. DB-setup readiness is observable in
    the logs (``sandbox_test_db_setup_ok`` / ``sandbox_test_db_setup_failed``),
    not in this bool.

    Every outcome is logged so a ``False`` (or a degraded DB setup) is always
    diagnosable — which step failed, its exit code, and a scrubbed stderr tail.

    #692 — a session may declare ``provisions_venv = False``: a client_attach box
    runs commands in the FOUNDER's own working directory, where ``uv sync`` is an
    unasked-for mutation of their tree and unnecessary anyway (their toolchain is
    already set up — they work there). Such a box is skipped without dispatching
    ANYTHING, and readiness is honestly ``False`` so no caller prepends a
    ``.venv/bin`` that this function did not create. Verify's
    ``_ensure_project_venv`` calls this same function, so the skip covers the
    gate path too: its commands run bare in the founder's own environment.
    """
    if not getattr(box, "provisions_venv", True):
        logger.info("sandbox_venv_provisioning_skipped", reason="session_does_not_provision")
        return False
    try:
        lock = await box.read_file("uv.lock", 64)
    except SandboxError:
        logger.info("sandbox_venv_no_lockfile", reason="read_error")
        return False
    if not lock:
        logger.info("sandbox_venv_no_lockfile", reason="empty")
        return False
    sync = await box.exec(_UV_SYNC, timeout_s=VENV_SYNC_TIMEOUT_S, shell=True)
    if sync.exit_code != 0 or sync.timed_out:
        logger.warning(
            "sandbox_venv_sync_failed",
            exit_code=sync.exit_code,
            timed_out=sync.timed_out,
            stderr_tail=_scrub(sync.stderr)[-500:],
        )
        return False
    logger.info("sandbox_venv_synced")
    # Optional test-DB provisioning (e.g. ``uv run alembic upgrade head``) after
    # the venv is ready — the injected BSVIBE_MIGRATION_DATABASE_URL is visible
    # to ``docker exec`` since it lives on the sandbox container. Best-effort +
    # logged: a setup failure is recorded but does NOT flip venv-readiness (the
    # venv IS ready), so the caller no longer misreads a migration failure as an
    # unusable environment.
    setup_cmd = get_settings().sandbox_test_db_setup_cmd
    if setup_cmd:
        setup = await box.exec(setup_cmd, timeout_s=TEST_DB_SETUP_TIMEOUT_S, shell=True)
        if setup.exit_code != 0 or setup.timed_out:
            logger.warning(
                "sandbox_test_db_setup_failed",
                exit_code=setup.exit_code,
                timed_out=setup.timed_out,
                stderr_tail=_scrub(setup.stderr)[-500:],
            )
        else:
            logger.info("sandbox_test_db_setup_ok")
    return True
