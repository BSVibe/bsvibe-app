"""/api/v1/workers — external executor-worker registration (Lift 1).

Three auth axes live behind one URL prefix:

* JWT + ``require_role("admin")`` — minting an install token, listing, revoking.
  These hang off ``router`` (mounted under the JWT-gated v1 aggregate in
  :mod:`backend.api.v1`).
* Install-token (``X-Install-Token`` header) — worker registration.
* Worker-token (``X-Worker-Token`` header) — heartbeat.

The last two are NOT JWT-authed (a headless worker machine has no Supabase
session), so they hang off ``public_router`` which :mod:`backend.api.main`
mounts at ``/api/v1/workers`` directly — bypassing the v1 router's
``get_current_user`` gate, exactly like the connector ``webhooks`` ingress.

:func:`get_current_worker` is the reusable worker-auth dependency later lifts
(poll / result) import.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_role
from backend.config import get_settings
from backend.executors import dispatch, service
from backend.executors.db import WorkerRow

# JWT-gated routes — mounted under the v1 aggregate (get_current_user upstream).
router = APIRouter()
# Token-authed routes — mounted at /api/v1/workers directly (no JWT gate).
public_router = APIRouter(prefix="/workers", tags=["workers"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class InstallTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


class WorkerRegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    labels: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class WorkerRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    token: str


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class WorkerResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    success: bool
    output: str = ""
    error_message: str | None = None


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    labels: list[str]
    capabilities: list[str]
    status: str
    is_active: bool

    @classmethod
    def from_row(cls, row: WorkerRow) -> WorkerResponse:
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            name=row.name,
            labels=list(row.labels or []),
            capabilities=list(row.capabilities or []),
            status=row.status,
            is_active=row.is_active,
        )


# ── Worker-token auth dependency (reusable by later lifts) ───────────────────


async def get_current_worker(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_worker_token: Annotated[str | None, Header()] = None,
) -> WorkerRow:
    """Authenticate a worker via ``X-Worker-Token``; 401 on missing/invalid.

    Later lifts (poll / result) depend on this to bind a request to its worker.
    """
    if not x_worker_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-Worker-Token"
        )
    worker = await service.authenticate_worker(session, x_worker_token)
    if worker is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid worker token")
    return worker


async def get_poll_redis() -> Any:
    """Build the Redis client the poll endpoint consumes the worker stream from.

    Returns a connection-lazy ``redis.asyncio`` client from ``settings.redis_url``
    (``decode_responses=True`` so stream fields are ``str``). Tests override this
    dependency with a ``fakeredis`` double, so the dispatch substrate is proven
    without a real Redis. A 503 surfaces only if redis cannot be imported.
    """
    try:
        import redis.asyncio as redis_aio  # noqa: PLC0415 — only needed on the poll path
    except ImportError as exc:  # pragma: no cover - redis is a declared dep
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis not available for worker dispatch",
        ) from exc
    return redis_aio.from_url(get_settings().redis_url, decode_responses=True)


# ── JWT-gated (admin / workspace-scoped) ──────────────────────────────────────


@router.post(
    "/install-token",
    response_model=InstallTokenResponse,
    dependencies=[Depends(require_role("admin"))],
)
async def mint_install_token(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> InstallTokenResponse:
    """Mint a new install token for the caller's workspace (replaces any prior).

    Returns the plaintext exactly once — it is never retrievable again.
    """
    token = await service.mint_install_token(session, workspace_id=workspace_id)
    await session.commit()
    return InstallTokenResponse(token=token)


@router.get("")
async def list_workers(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[WorkerResponse]:
    """List active workers for the caller's workspace."""
    rows = await service.list_workers(session, workspace_id)
    return [WorkerResponse.from_row(r) for r in rows]


@router.delete("/{worker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_worker(
    worker_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Soft-delete a worker (workspace-scoped). 404 when not found here."""
    row = await service.revoke_worker(session, workspace_id=workspace_id, worker_id=worker_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Worker {worker_id} not found"
        )
    await session.commit()


# ── Token-authed (public_router — no JWT gate) ────────────────────────────────


@public_router.post(
    "/register",
    response_model=WorkerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_worker(
    body: WorkerRegisterBody,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_install_token: Annotated[str | None, Header()] = None,
) -> WorkerRegisterResponse:
    """Register a worker using an ``X-Install-Token`` header.

    Admins mint the install token via ``POST /api/v1/workers/install-token``
    and share it with worker machines. Returns the worker id + a fresh
    per-worker token (used for heartbeat / future poll+result).
    """
    if not x_install_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-Install-Token"
        )
    try:
        worker, token = await service.register_worker(
            session,
            install_token=x_install_token,
            name=body.name,
            labels=body.labels,
            capabilities=body.capabilities,
        )
    except service.InvalidInstallToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid install token"
        ) from exc
    await session.commit()
    return WorkerRegisterResponse(id=worker.id, token=token)


@public_router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    worker: Annotated[WorkerRow, Depends(get_current_worker)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> HeartbeatResponse:
    """Record a worker heartbeat — sets status online + stamps last_heartbeat."""
    await service.record_heartbeat(session, worker)
    await session.commit()
    return HeartbeatResponse(status="ok")


@public_router.post("/poll")
async def poll_tasks(
    worker: Annotated[WorkerRow, Depends(get_current_worker)],
    redis: Annotated[Any, Depends(get_poll_redis)],
    count: int = 1,
) -> list[dict[str, Any]]:
    """Drain up to ``count`` dispatched tasks off the worker's Redis stream.

    XREADGROUP over the worker's dedicated ``tasks:worker:{id}`` stream (group
    ``worker-{id}``, consumer ``worker-{id}-0``), auto-acking each message so a
    second poll returns only newer entries. Each returned message is the flat
    dispatch payload (task_id / executor_type / prompt / system / workspace_dir /
    stream_channel / done_channel / action / dispatched_at).
    """
    from backend.workers.streams import RedisStreamConsumer  # noqa: PLC0415

    stream_name = dispatch.worker_stream(worker.id)
    group = f"worker-{worker.id}"
    consumer = f"worker-{worker.id}-0"

    collected: list[dict[str, Any]] = []

    async def _collect(fields: dict[str, Any]) -> None:
        collected.append(fields)

    consumer_obj = RedisStreamConsumer(redis)
    await consumer_obj.consume_once(
        stream_name=stream_name,
        consumer_group=group,
        consumer_name=consumer,
        handler=_collect,
        count=max(1, count),
    )
    return collected


@public_router.post("/result", response_model=HeartbeatResponse)
async def report_result(
    body: WorkerResultBody,
    worker: Annotated[WorkerRow, Depends(get_current_worker)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[Any, Depends(get_poll_redis)],
) -> HeartbeatResponse:
    """Record a worker's task result — flips the task row to done / failed.

    A remote worker reaches the backend only over HTTP and usually cannot
    publish the ``task:{id}:done`` channel itself. The backend owns redis, so
    :func:`dispatch.record_result` publishes the authoritative completion signal
    here (after the row flips terminal) — waking any orchestrator awaiting on it
    promptly instead of letting it block until its timeout.
    """
    _ = worker  # auth only; the task row carries its own workspace binding
    await dispatch.record_result(
        session,
        redis,
        task_id=body.task_id,
        success=body.success,
        output=body.output,
        error_message=body.error_message,
    )
    await session.commit()
    return HeartbeatResponse(status="ok")


__all__ = ["get_current_worker", "get_poll_redis", "public_router", "router"]
