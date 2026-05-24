"""Service layer for the external executor-worker registration subsystem.

Ported from BSGateway's ``executor/install_token.py`` + the inline logic in
``api/routers/workers.py``, adapted to async SQLAlchemy and ``workspace_id``.

Token model (matches BSGateway):
  * **install token** — per-workspace, minted by an admin, used by worker
    machines to register. One active token per workspace; re-minting replaces.
  * **worker token** — per-worker, minted at registration, used on every
    subsequent worker request (heartbeat, and — in later lifts — poll/result).

Only SHA-256 hashes are persisted; plaintext is returned once and never stored.
The session is owned by the caller (router / test) — these functions ``add``/
``delete`` and ``flush`` but never ``commit``, so the caller controls the
transaction boundary.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.executors.db import WorkerInstallTokenRow, WorkerRow

logger = structlog.get_logger(__name__)


class InvalidInstallToken(Exception):
    """Raised when registration is attempted with a missing/unknown install token."""


def _hash_token(token: str) -> str:
    """SHA-256 hex digest — the single hashing primitive for both token kinds."""
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


# ── Install token ───────────────────────────────────────────────────────────


async def mint_install_token(session: AsyncSession, *, workspace_id: uuid.UUID) -> str:
    """Mint a new install token for ``workspace_id``, returning the plaintext once.

    Idempotent on the workspace: any prior install token is deleted first, so
    a workspace has at most one active install token at a time (re-mint = rotate).
    """
    await session.execute(
        delete(WorkerInstallTokenRow).where(WorkerInstallTokenRow.workspace_id == workspace_id)
    )
    token = _generate_token()
    session.add(WorkerInstallTokenRow(workspace_id=workspace_id, token_hash=_hash_token(token)))
    await session.flush()
    logger.info("executor_install_token_minted", workspace_id=str(workspace_id))
    return token


async def get_install_token_hash(session: AsyncSession, workspace_id: uuid.UUID) -> str | None:
    """Return the stored install-token hash for ``workspace_id``, or ``None``."""
    row = (
        await session.execute(
            select(WorkerInstallTokenRow).where(WorkerInstallTokenRow.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    return row.token_hash if row is not None else None


async def resolve_install_token_workspace(session: AsyncSession, token: str) -> uuid.UUID | None:
    """Find the workspace that minted ``token``, or ``None`` if unknown."""
    if not token:
        return None
    row = (
        await session.execute(
            select(WorkerInstallTokenRow).where(
                WorkerInstallTokenRow.token_hash == _hash_token(token)
            )
        )
    ).scalar_one_or_none()
    return row.workspace_id if row is not None else None


# ── Worker registration ───────────────────────────────────────────────────────


async def register_worker(
    session: AsyncSession,
    *,
    install_token: str,
    name: str,
    labels: list[str],
    capabilities: list[str],
) -> tuple[WorkerRow, str]:
    """Validate ``install_token`` and create a worker, returning ``(row, plaintext)``.

    The fresh per-worker token's plaintext is returned once; only its hash is
    persisted. Raises :class:`InvalidInstallToken` when the install token is
    absent or does not match any workspace.
    """
    workspace_id = await resolve_install_token_workspace(session, install_token)
    if workspace_id is None:
        raise InvalidInstallToken("invalid or missing install token")

    token = _generate_token()
    worker = WorkerRow(
        workspace_id=workspace_id,
        name=name,
        labels=list(labels),
        capabilities=list(capabilities),
        status="offline",
        token_hash=_hash_token(token),
        is_active=True,
    )
    session.add(worker)
    await session.flush()
    logger.info(
        "executor_worker_registered",
        worker_id=str(worker.id),
        workspace_id=str(workspace_id),
        name=name,
    )
    return worker, token


async def authenticate_worker(session: AsyncSession, token: str) -> WorkerRow | None:
    """Resolve an active worker by its plaintext token, or ``None``."""
    if not token:
        return None
    row = (
        await session.execute(
            select(WorkerRow).where(
                WorkerRow.token_hash == _hash_token(token),
                WorkerRow.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return row


async def record_heartbeat(session: AsyncSession, worker: WorkerRow) -> WorkerRow:
    """Mark ``worker`` online and stamp ``last_heartbeat`` to now."""
    worker.status = "online"
    worker.last_heartbeat = datetime.now(UTC)
    await session.flush()
    return worker


async def list_workers(session: AsyncSession, workspace_id: uuid.UUID) -> list[WorkerRow]:
    """List active workers for ``workspace_id``, newest first."""
    rows = (
        (
            await session.execute(
                select(WorkerRow)
                .where(
                    WorkerRow.workspace_id == workspace_id,
                    WorkerRow.is_active.is_(True),
                )
                .order_by(WorkerRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def revoke_worker(
    session: AsyncSession, *, workspace_id: uuid.UUID, worker_id: uuid.UUID
) -> WorkerRow | None:
    """Soft-delete a worker (``is_active=False``), workspace-scoped.

    Returns the row on success, or ``None`` when no active worker with that id
    exists in ``workspace_id`` (cross-workspace revoke is a no-op).
    """
    row = (
        await session.execute(
            select(WorkerRow).where(
                WorkerRow.id == worker_id,
                WorkerRow.workspace_id == workspace_id,
                WorkerRow.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.is_active = False
    await session.flush()
    logger.info(
        "executor_worker_revoked",
        worker_id=str(worker_id),
        workspace_id=str(workspace_id),
    )
    return row


__all__ = [
    "InvalidInstallToken",
    "authenticate_worker",
    "get_install_token_hash",
    "list_workers",
    "mint_install_token",
    "record_heartbeat",
    "register_worker",
    "resolve_install_token_workspace",
    "revoke_worker",
]
