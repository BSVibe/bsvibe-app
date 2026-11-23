"""Stage A replay as a background job — register, claim, run, read.

A replay cannot finish inside an MCP request: one Stage A round alone is up to 180 s and
Cloudflare cuts a silent request at ~100 s (see ``reindex_tools.py``). So the MCP tool only
registers a row in ``adversarial_replay_jobs`` and answers with its id; a worker process runs it;
a status tool reads it. The row — not an in-process lock — is the state, so it survives a restart
and holds across processes:

* **One at a time.** :func:`claim_next` hands out a queued job only when no other job is
  ``running`` with a live heartbeat. A ``running`` job whose heartbeat is older than
  :data:`LEASE_S` (its worker died) is failed first, so the slot never stays blocked.
* **One case failing is only that case.** :func:`run_replay` records any exception as that case's
  ``failed`` (with the reason) and moves on; the job still finishes ``done``.
* **Nothing but the job row is written.** No run, deliverable, card, ledger entry or knowledge —
  and the judge LLM is resolved with no run id (see :func:`judge_llm_for`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import cross_tenant_session_read, workspace_session_scope
from backend.workflow.application.adversarial_replay import (
    BoxFactory,
    CaseSpec,
    ReplayRefused,
    StageA,
    run_replay,
    score,
    validate_request,
)
from backend.workflow.application.adversarial_verifier import run_adversarial_round
from backend.workflow.infrastructure.db import AdversarialReplayJob

logger = structlog.get_logger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

#: A running job that has not beaten for this long lost its worker. One Stage A round is capped at
#: 180 s and the beat fires before every round, so this leaves a wide margin.
LEASE_S = 900.0


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite hands timestamps back naive
    return value


def _is_live(job: AdversarialReplayJob, now: datetime) -> bool:
    beat = _aware(job.heartbeat_at) or _aware(job.started_at)
    return beat is not None and (now - beat) < timedelta(seconds=LEASE_S)


# ---------------------------------------------------------------------------
# register (MCP side)
# ---------------------------------------------------------------------------
async def submit_job(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    specs: list[CaseSpec],
    *,
    repeat: int,
    batch: bool,
) -> AdversarialReplayJob:
    """Register one replay. Refused (:class:`ReplayRefused`) when this workspace already has one
    queued or running — the owner's tokens pay for it, one at a time."""
    validate_request(len(specs), repeat)
    now = _now()
    active = (
        (
            await session.execute(
                select(AdversarialReplayJob).where(
                    AdversarialReplayJob.workspace_id == workspace_id,
                    AdversarialReplayJob.status.in_((QUEUED, RUNNING)),
                )
            )
        )
        .scalars()
        .all()
    )
    for job in active:
        if job.status == QUEUED or _is_live(job, now):
            raise ReplayRefused(
                f"다른 재생({job.id})이 이미 {job.status} 상태다 — 끝난 뒤 다시 시도하라"
            )
    job = AdversarialReplayJob(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=QUEUED,
        request={
            "repeat": repeat,
            "batch": batch,
            "cases": [
                {
                    "deliverable_id": str(s.deliverable_id),
                    "expected": s.expected,
                    "defect_note": s.defect_note,
                }
                for s in specs
            ],
        },
        results=[],
        created_at=now,
    )
    session.add(job)
    await session.commit()
    return job


# ---------------------------------------------------------------------------
# read (MCP side)
# ---------------------------------------------------------------------------
async def job_status(
    session: AsyncSession, workspace_id: uuid.UUID, replay_id: uuid.UUID
) -> dict[str, Any] | None:
    """The job as the status tool reports it; ``None`` when it is not this workspace's."""
    job = await session.get(AdversarialReplayJob, replay_id)
    if job is None or job.workspace_id != workspace_id:
        return None
    request = job.request if isinstance(job.request, dict) else {}
    results = list(job.results or [])
    total = len(request.get("cases") or [])
    out: dict[str, Any] = {
        "replay_id": str(job.id),
        "status": job.status,
        "batch": bool(request.get("batch")),
        "repeat": request.get("repeat"),
        "progress": {"cases_total": total, "cases_done": len(results)},
        "results": results,
        # the totals over what has finished so far; final once status is done
        "summary": job.summary
        if job.summary is not None
        else (score(results) if results else None),
        "error": job.error,
        "created_at": _aware(job.created_at),
        "started_at": _aware(job.started_at),
        "finished_at": _aware(job.finished_at),
    }
    if job.status == RUNNING and not _is_live(job, _now()):
        out["stalled"] = True  # the worker stopped beating; the next claim fails it
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# claim + run (worker side)
# ---------------------------------------------------------------------------
async def claim_next(
    session_factory: async_sessionmaker[AsyncSession],
) -> AdversarialReplayJob | None:
    """Take the oldest queued job — only when no other job is running with a live heartbeat."""
    now = _now()
    async with session_factory() as session:
        async with cross_tenant_session_read(session):
            running = (
                (
                    await session.execute(
                        select(AdversarialReplayJob).where(AdversarialReplayJob.status == RUNNING)
                    )
                )
                .scalars()
                .all()
            )
            busy = False
            for job in running:
                if _is_live(job, now):
                    busy = True
                    continue
                job.status = FAILED
                job.error = "워커가 멈췄다 — 하트비트가 끊긴 재생을 실패로 닫았다"
                job.finished_at = now
            if busy:
                await session.commit()
                return None
            candidate = (
                await session.execute(
                    select(AdversarialReplayJob)
                    .where(AdversarialReplayJob.status == QUEUED)
                    .order_by(AdversarialReplayJob.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if candidate is None:
                await session.commit()
                return None
            # compare-and-set: of several workers, exactly one flips queued → running
            claimed = await session.execute(
                update(AdversarialReplayJob)
                .where(
                    AdversarialReplayJob.id == candidate.id,
                    AdversarialReplayJob.status == QUEUED,
                )
                .values(status=RUNNING, started_at=now, heartbeat_at=now)
            )
            await session.commit()
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return None
            await session.refresh(candidate)
            return candidate


async def _write(
    session_factory: async_sessionmaker[AsyncSession],
    job: AdversarialReplayJob,
    **values: Any,
) -> None:
    """One short write to this job's row, inside its workspace's scope."""
    async with session_factory() as session:
        async with workspace_session_scope(session, job.workspace_id):
            await session.execute(
                update(AdversarialReplayJob)
                .where(AdversarialReplayJob.id == job.id)
                .values(**values)
            )
            await session.commit()


LlmFor = Callable[[uuid.UUID], Awaitable[Any]]
LoadFor = Callable[[uuid.UUID], Callable[[uuid.UUID], Awaitable[Any]]]


async def execute_job(
    session_factory: async_sessionmaker[AsyncSession],
    job: AdversarialReplayJob,
    *,
    llm_for: LlmFor,
    load_for: LoadFor,
    box_for: BoxFactory,
    work_root: Path,
    stage_a: StageA = run_adversarial_round,
) -> None:
    """Run a claimed job to the end and record it. Never raises: a job-level failure is the row's
    ``failed`` + ``error``; a case-level one is that case's ``failed`` inside ``results``."""
    request = job.request if isinstance(job.request, dict) else {}
    try:
        specs = [
            CaseSpec(uuid.UUID(c["deliverable_id"]), c.get("expected"), c.get("defect_note"))
            for c in request.get("cases") or []
        ]
        llm = await llm_for(job.workspace_id)
        if llm is None:
            raise ReplayRefused("이 워크스페이스에 judge 모델 경로가 없어 재생할 수 없다")

        async def progress(results: list[dict[str, Any]]) -> None:
            await _write(session_factory, job, results=list(results), heartbeat_at=_now())

        async def beat() -> None:
            await _write(session_factory, job, heartbeat_at=_now())

        out = await run_replay(
            specs,
            repeat=int(request.get("repeat") or 1),
            load=load_for(job.workspace_id),
            llm=llm,
            box_for=box_for,
            work_root=work_root,
            stage_a=stage_a,
            on_progress=progress,
            beat=beat,
        )
        await _write(
            session_factory,
            job,
            status=DONE,
            results=out["results"],
            summary=out["summary"],
            finished_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001 — a job must always end in a recorded state
        logger.warning("adversarial_replay_job_failed", job_id=str(job.id), error=repr(exc)[:300])
        await _write(
            session_factory,
            job,
            status=FAILED,
            error=f"{type(exc).__name__}: {str(exc)[:500]}",
            finished_at=_now(),
        )


__all__ = [
    "DONE",
    "FAILED",
    "LEASE_S",
    "QUEUED",
    "RUNNING",
    "claim_next",
    "execute_job",
    "job_status",
    "submit_job",
]
