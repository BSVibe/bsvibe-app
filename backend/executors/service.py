"""Service layer for the external executor-worker registration subsystem.

Token model:
  * **worker token** — per-worker, minted at registration, used on every
    subsequent worker request (heartbeat, poll, result).

Only SHA-256 hashes are persisted; plaintext is returned once and never stored.
The session is owned by the caller (router / test) — these functions ``add``/
``delete`` and ``flush`` but never ``commit``, so the caller controls the
transaction boundary.

Lift E5 (2026-06-06) — the legacy ``install_token`` system is gone. The CLI
now registers with the host's OAuth bearer (Supabase session JWT or MCP
access token, Lift E4); the workspace is derived from verified claims, not a
DB-stored install token. ``mint_install_token`` /
``resolve_install_token_workspace`` / ``InvalidInstallToken`` and the
``register_worker(install_token=…)`` overload are all removed.

Lift M2 (v8 §20.3 Pattern B audit, 2026-06-02) — module-level function
decomposition (Pattern E). Token-hashing primitives (``_hash_token``,
``_generate_token``) are module-level helpers; executor-model-account
persistence is delegated to ``ModelAccountRepository`` (Lift I-Repo seam).
Each function has a single, narrow responsibility.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.executors.db import WorkerRow
from backend.router.accounts.account_service import ensure_personal_account
from backend.router.accounts.predicates import EXECUTOR_PROVIDER
from backend.router.accounts.repository import ModelAccountRepository

logger = structlog.get_logger(__name__)


def _hash_token(token: str) -> str:
    """SHA-256 hex digest — the worker-token hashing primitive."""
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


# ── Executor model accounts (Lift 5a) ─────────────────────────────────────────


def _executor_label(name: str, capability: str, *, single: bool) -> str:
    """A single-capability worker borrows its name; multi disambiguates."""
    return name if single else f"{name} ({capability})"


async def _upsert_executor_model_accounts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    worker_id: uuid.UUID,
    name: str,
    capabilities: list[str],
) -> None:
    """Make each worker capability a routable ``provider='executor'`` model row.

    BSGateway's "abstract the coding agent like an LLM" pattern: one row per
    capability so the existing model-resolution treats it like any model (Lift
    5b branches on ``provider=='executor'`` to dispatch to the worker). The row
    carries NO api key — it is inserted via the low-level repository directly,
    NEVER through ``ModelAccountService.create`` (which would encrypt a key it
    doesn't have).

    Idempotent on ``(worker_id, capability)``: re-register / re-mint of the same
    worker reuses the existing rows (keyed by the ``extra_params.worker_id`` tag
    plus the capability) instead of duplicating them.
    """
    repo = ModelAccountRepository(session)
    existing = await repo.list_executor_accounts_for_worker(
        workspace_id=workspace_id, worker_id=worker_id
    )
    by_capability = {r.extra_params.get("executor_type"): r for r in existing}
    single = len(capabilities) == 1
    for capability in capabilities:
        if capability in by_capability:
            continue  # already routable — idempotent re-register
        await repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            provider=EXECUTOR_PROVIDER,
            label=_executor_label(name, capability, single=single),
            litellm_model=f"executor/{capability}",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            extra_params={"worker_id": str(worker_id), "executor_type": capability},
        )


async def _remove_executor_model_accounts(
    session: AsyncSession, *, workspace_id: uuid.UUID, worker_id: uuid.UUID
) -> None:
    """Delete the routable executor model rows bound to ``worker_id``."""
    repo = ModelAccountRepository(session)
    rows = await repo.list_executor_accounts_for_worker(
        workspace_id=workspace_id, worker_id=worker_id
    )
    for row in rows:
        await session.delete(row)
    await session.flush()


# ── Worker registration ───────────────────────────────────────────────────────


async def register_worker_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    name: str,
    labels: list[str],
    capabilities: list[str],
) -> tuple[WorkerRow, str]:
    """Create a worker bound to ``workspace_id``, returning ``(row, plaintext)``.

    The workspace is derived upstream from the OAuth bearer (Supabase session
    JWT or MCP access token) so no install-token round-trip is required. The
    fresh per-worker token's plaintext is returned once; only its hash is
    persisted.
    """
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

    # Make each capability a routable provider='executor' model account
    # (Lift 5a). The personal account is the partition the rows hang off.
    account = await ensure_personal_account(session, workspace_id=workspace_id)
    await _upsert_executor_model_accounts(
        session,
        workspace_id=workspace_id,
        account_id=account.id,
        worker_id=worker.id,
        name=name,
        capabilities=list(capabilities),
    )

    logger.info(
        "executor_worker_registered",
        worker_id=str(worker.id),
        workspace_id=str(workspace_id),
        name=name,
        capabilities=list(capabilities),
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
    # Remove the routable executor model accounts so a revoked worker is no
    # longer resolvable (Lift 5a).
    await _remove_executor_model_accounts(session, workspace_id=workspace_id, worker_id=worker_id)
    await session.flush()
    logger.info(
        "executor_worker_revoked",
        worker_id=str(worker_id),
        workspace_id=str(workspace_id),
    )
    return row


__all__ = [
    "authenticate_worker",
    "list_workers",
    "record_heartbeat",
    "register_worker_for_workspace",
    "revoke_worker",
]
