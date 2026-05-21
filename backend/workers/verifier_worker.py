"""VerifierWorker — execute verification contracts in sandbox.

Workflow §12.5 #8 (Bundle G — Workers). Lifts from BSNexus
Part-B per-project DinD work-sandbox verifier (memory:
project_bsnexus_verification_contract).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class VerifierWorker:
    """Consumer-group worker for the ``verifier`` Redis Stream."""

    async def start(self) -> None:
        """Drive verification: pull deliverable → run contract → record."""
        # TODO(bundle-g-integration): lift from BSNexus
        # backend/workers/verifier_worker.py — runs declared_command +
        # llm_judge aspects against the per-project sandbox.
        logger.debug("verifier_worker_start_stub")
        raise NotImplementedError("VerifierWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain."""
        # TODO(bundle-g-integration): cancel + close.
        logger.debug("verifier_worker_stop_stub")
        raise NotImplementedError("VerifierWorker.stop pending Bundle G integration")


__all__ = ["VerifierWorker"]
