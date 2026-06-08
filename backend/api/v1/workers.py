"""/api/v1/workers — external executor-worker registration.

Three auth axes live behind one URL prefix:

* JWT — listing, revoking. Workspace-scoped via the v1 aggregate's
  ``get_current_user`` + per-route ``get_workspace_id``. These hang off
  ``router`` (mounted under the JWT-gated v1 aggregate in
  :mod:`backend.api.v1`).
* OAuth bearer (``Authorization: Bearer <token>``) — worker registration
  (Lift E4). The bearer is either a Supabase session JWT (the same one the
  PWA uses) or an ES256 MCP access token (Lift D1). Workspace is derived
  from the verified claims — the body never carries a workspace id. This is
  the only register path; the legacy ``X-Install-Token`` header was removed
  in Lift E5.
* Worker-token (``X-Worker-Token`` header) — heartbeat / poll / result.

The bearer / worker-token paths are NOT JWT-authed (a headless worker
machine has no Supabase session by default), so they hang off
``public_router`` which :mod:`backend.api.main` mounts at ``/api/v1/workers``
directly — bypassing the v1 router's ``get_current_user`` gate, exactly like
the connector ``webhooks`` ingress.

:func:`get_current_worker` is the reusable worker-auth dependency the poll /
result endpoints import.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_artifact_store, get_db_session, get_workspace_id
from backend.api.v1.workers_register_auth import (
    BearerAuthError,
    extract_bearer,
    resolve_workspace_for_bearer,
)
from backend.config import get_settings
from backend.executors import dispatch, service
from backend.executors.db import WorkerRow
from backend.storage.artifact_store import ArtifactStore

# JWT-gated routes — mounted under the v1 aggregate (get_current_user upstream).
router = APIRouter()
# Token-authed routes — mounted at /api/v1/workers directly (no JWT gate).
public_router = APIRouter(prefix="/workers", tags=["workers"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class WorkerRegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    labels: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class WorkerRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    token: str


class HeartbeatBody(BaseModel):
    """Lift E16 — capacity-aware dispatch heartbeat payload.

    ``in_flight`` is the worker's ``len(in_flight)`` at the moment of the
    heartbeat. The backend stamps it onto :attr:`WorkerRow.last_in_flight`
    so :func:`find_available_worker` can exclude saturated workers from
    selection — otherwise the backend dispatches onto a stream the
    worker's poll loop has paused (at-cap), the 600 s
    ``await_completion`` timer expires before the worker reads the
    task, and chunks are marked ``failed`` before they ever run.

    The body itself is optional (an older worker shape POSTs no body at
    all) — when omitted, the field defaults to ``0`` so the back-compat
    rollout path remains functional.
    """

    model_config = ConfigDict(extra="forbid")

    in_flight: int = Field(default=0, ge=0)


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class WorkerResultFile(BaseModel):
    """One file the worker's CLI produced, shipped back for persistence (B1)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1)
    content_b64: str = ""
    truncated: bool = False


class WorkerResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    success: bool
    output: str = ""
    error_message: str | None = None
    # Files the CLI produced (executor-pool B1). Persisted under the run
    # workspace + recorded as the task's artifact_refs by ``record_result``.
    files: list[WorkerResultFile] = Field(default_factory=list)


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    labels: list[str]
    capabilities: list[str]
    status: str
    is_active: bool
    # Lift E4 — expose heartbeat + creation timestamps so the PWA Workers tab
    # can render the GitHub-Actions-runner-style detail (last seen / added on).
    last_heartbeat: str | None = None
    # Lift E13 — fleet-detail field. ``status="online"`` can lie when the
    # worker process died before clearing the column; ``heartbeat_fresh``
    # mirrors the predicate ``find_available_worker`` uses, so the founder
    # can spot a stale-online row at a glance.
    heartbeat_fresh: bool = False
    created_at: str | None = None

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
            last_heartbeat=row.last_heartbeat.isoformat() if row.last_heartbeat else None,
            heartbeat_fresh=dispatch.is_heartbeat_fresh(row.last_heartbeat),
            created_at=row.created_at.isoformat() if row.created_at else None,
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


# ── JWT-gated (workspace-scoped) ──────────────────────────────────────────────


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
    authorization: Annotated[str | None, Header()] = None,
) -> WorkerRegisterResponse:
    """Register a worker with an OAuth bearer (Lift E4).

    The bearer is either a Supabase session JWT (the same one the PWA uses)
    or an ES256 MCP access token (Lift D1). Workspace is derived from the
    verified claims; the body's ``name`` / ``labels`` / ``capabilities`` are
    accepted verbatim.

    Returns the worker id + a fresh per-worker token (used for heartbeat /
    poll / result). The worker-token plaintext is returned ONCE; only its
    hash is persisted.

    Lift E5 (2026-06-06) — the legacy ``X-Install-Token`` header path is
    gone. Missing ``Authorization: Bearer`` → 401.
    """
    bearer = extract_bearer(authorization)
    if bearer is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization bearer",
        )
    try:
        principal = await resolve_workspace_for_bearer(bearer, session)
    except BearerAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        ) from exc
    worker, token = await service.register_worker_for_workspace(
        session,
        workspace_id=principal.workspace_id,
        name=body.name,
        labels=body.labels,
        capabilities=body.capabilities,
    )
    await session.commit()
    return WorkerRegisterResponse(id=worker.id, token=token)


@public_router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    worker: Annotated[WorkerRow, Depends(get_current_worker)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    body: Annotated[HeartbeatBody | None, Body()] = None,
) -> HeartbeatResponse:
    """Record a worker heartbeat — sets status online + stamps last_heartbeat.

    Lift E16 — the optional ``HeartbeatBody`` carries the worker's
    current ``in_flight`` task count which the backend persists onto
    :attr:`WorkerRow.last_in_flight` for capacity-aware dispatch. An
    empty body (older worker shape) defaults to ``in_flight=0`` so the
    back-compat path remains functional.
    """
    body = body or HeartbeatBody()
    await service.record_heartbeat(session, worker, in_flight=body.in_flight)
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
    artifact_store: Annotated[ArtifactStore, Depends(get_artifact_store)],
) -> HeartbeatResponse:
    """Record a worker's task result — flips the task row to done / failed.

    A remote worker reaches the backend only over HTTP and usually cannot
    publish the ``task:{id}:done`` channel itself. The backend owns redis, so
    :func:`dispatch.record_result` publishes the authoritative completion signal
    here (after the row flips terminal) — waking any orchestrator awaiting on it
    promptly instead of letting it block until its timeout.

    The per-run :class:`ArtifactStore` is injected via deps (swap-ready for
    R2/S3) — the worker's returned files are persisted through it.
    """
    _ = worker  # auth only; the task row carries its own workspace binding
    await dispatch.record_result(
        session,
        redis,
        task_id=body.task_id,
        success=body.success,
        output=body.output,
        error_message=body.error_message,
        files=[f.model_dump() for f in body.files],
        artifact_store=artifact_store,
    )
    await session.commit()
    return HeartbeatResponse(status="ok")


__all__ = ["get_current_worker", "get_poll_redis", "public_router", "router"]
