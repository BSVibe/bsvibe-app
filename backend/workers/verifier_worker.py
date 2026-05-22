"""VerifierWorker — execute pending verifications + persist results.

Workflow §12.5 #8 (Bundle G — Workers). Pulls WorkStep rows in the
``running`` state, hands each to a caller-supplied verifier (typically
:class:`backend.execution.verifier.contract.VerificationContract`-backed),
and writes a VerificationResult row + flips WorkStep.proof_state.

The verifier adapter is a Protocol so tests can pass a fake; production
wiring (Bundle G) constructs the real verifier from a parsed
``backend.execution.verifier.contract.VerificationContract``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.execution.db import (
    ProofState,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)


class VerifierAdapter(Protocol):
    """Run a single verification check for ``work_step``."""

    async def verify(
        self, *, work_step: WorkStep
    ) -> tuple[VerificationOutcome, dict[str, Any]]: ...


@dataclass(slots=True)
class VerifierConfig:
    batch_size: int = 5
    poll_interval_s: float = 5.0


class VerifierWorker(BaseWorker):
    """Periodic drain of pending WorkSteps → VerificationResult rows."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        verifier: VerifierAdapter,
        config: VerifierConfig | None = None,
    ) -> None:
        self._cfg = config or VerifierConfig()
        super().__init__(name="verifier_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._verifier = verifier

    async def _tick(self) -> int:
        return await self.verify_once()

    async def verify_once(self) -> int:
        """Verify one batch of running WorkSteps. Returns count processed."""
        async with self._session_factory() as session:
            stmt = (
                select(WorkStep)
                .where(WorkStep.status == WorkStepStatus.RUNNING)
                .order_by(WorkStep.updated_at.asc())
                .limit(self._cfg.batch_size)
                .with_for_update(skip_locked=True)
            )
            steps = (await session.execute(stmt)).scalars().all()
            for step in steps:
                try:
                    outcome, result_dict = await self._verifier.verify(work_step=step)
                except Exception as exc:  # noqa: BLE001 — domain failure, not crash
                    logger.warning(
                        "verifier_step_failed",
                        work_step_id=str(step.id),
                        error=str(exc),
                    )
                    outcome = VerificationOutcome.INCONCLUSIVE
                    result_dict = {"error": str(exc)}
                session.add(
                    VerificationResult(
                        id=uuid.uuid4(),
                        run_id=step.run_id,
                        work_step_id=step.id,
                        workspace_id=step.workspace_id,
                        outcome=outcome,
                        contract={},
                        result=result_dict,
                        created_at=__import__("datetime").datetime.now(
                            tz=__import__("datetime").UTC
                        ),
                    )
                )
                step.proof_state = _outcome_to_proof(outcome)
                step.status = (
                    WorkStepStatus.VERIFIED
                    if outcome == VerificationOutcome.PASSED
                    else WorkStepStatus.REJECTED
                )
                step.updated_at = __import__("datetime").datetime.now(tz=__import__("datetime").UTC)
            await session.commit()
            return len(steps)


def _outcome_to_proof(outcome: VerificationOutcome) -> ProofState:
    if outcome == VerificationOutcome.PASSED:
        return ProofState.PROVED
    if outcome == VerificationOutcome.FAILED:
        return ProofState.REFUTED
    return ProofState.UNTESTED


__all__ = ["VerifierAdapter", "VerifierConfig", "VerifierWorker"]
