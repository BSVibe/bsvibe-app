"""#692 in-place verify — run a client_attach repo's OWN gate on its own machine.

A ``client_attach`` product's source lives only on the founder's machine, so the
server can prove nothing about it — which is why such a run has been ending at
``review_ready`` + ``proof_state=UNTESTED``. But the gate's honesty was never
about WHERE it ran: it is "the command's EXIT CODE is the verdict, never a
model's opinion". Move the command to the founder's machine and that property
holds exactly as before. Only the command and its exit code / output cross the
wire; the source does not.

Why this is a dedicated path rather than the normal ``verify``: a client_attach
agent acts with the CLI's NATIVE tools, so BSVibe's MCP work tools — and with
them ``declare_verification`` — are withheld. ``assemble_contract`` would always
return ``None`` and the run would end in a ``no_verification_declared``
decision: strictly worse than today. The DERIVED gate needs no declared
contract. It reads the repo's own manifests and derives the commands, so it is
the one verification mechanism that works here.

The ladder, fail-CLOSED at every rung:

* no manifest on that machine → genuinely gateless → ``None`` (UNTESTED stands).
* the deriver could not run → ``passed=False`` on a manifest-present repo, and
  never ``proved`` (a transient LLM failure must not become a silent proof).
* a command RAN and failed → an honest gate failure.
* a command was missing (exit 127) → ``unavailable``: not a failure, but it
  proves nothing either.
* at least one command RAN and passed, none failed → ``proved``.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.application.verification_service import (
    DerivedGateFailed,
    VerificationService,
)
from backend.workflow.domain.verifier_contract import VerificationContract
from backend.workflow.infrastructure.db import VerificationOutcome, VerificationResult
from backend.workflow.infrastructure.sandbox import SandboxSession

logger = structlog.get_logger(__name__)


async def run_inplace_gate(
    service: VerificationService,
    *,
    run: Any,
    box: SandboxSession,
) -> dict[str, Any] | None:
    """Derive and run this repo's verification gate on the founder's machine.

    Returns the gate blob (``passed`` / ``proved`` / per-command records), or
    ``None`` when the repo declares no toolchain at all — the one case where
    "no gate ran" is not a failure but simply the truth about the repo.

    The blob is persisted as a :class:`VerificationResult` so a ``PROVED`` run
    has inspectable evidence (what ran, with which exit codes) on the proof
    surface, exactly like a server-sandbox verify.
    """
    # The steps below are minutes of external work (an LLM derivation, then the
    # repo's own test/lint commands on a remote machine). Holding an open
    # transaction across them is the recurring BSVibe outage (#632/#686/#680):
    # the work succeeds and the RECORDING dies with PendingRollbackError. End
    # the transaction first — ``expire_on_commit=False`` keeps ORM state usable.
    await service._release_connection(run)

    manifests = await service._read_repo_manifests(box)
    if not manifests:
        logger.info("inplace_gate_no_manifest", run_id=str(run.id))
        return None

    payload = run.payload or {}
    intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
    gate = await service._author_derived_gate(intent, manifests, [])

    if isinstance(gate, DerivedGateFailed):
        # A manifest EXISTS, so a gate was expected here and we could not produce
        # one. Fail closed: not passed, and certainly not proved.
        blob: dict[str, Any] = {
            "origin": "derived_in_place",
            "applicable": True,
            "commands": [],
            "passed": False,
            "proved": False,
            "gate_deriver_failed": gate.reason,
        }
        await _persist(service, run=run, blob=blob, outcome=VerificationOutcome.FAILED)
        logger.info("inplace_gate_deriver_failed", run_id=str(run.id), reason=gate.reason)
        return blob

    if not gate.applicable or gate.is_empty:
        # The deriver ran and found nothing to run. Honest, but it proves nothing.
        blob = {
            "origin": "derived_in_place",
            "applicable": gate.applicable,
            "commands": [],
            "passed": True,
            "proved": False,
        }
        await _persist(service, run=run, blob=blob, outcome=VerificationOutcome.PASSED)
        return blob

    results = await _run_commands(service, gate=gate, box=box)
    passed = not any(r["status"] == "failed" for r in results)
    # PROVED needs something to have ACTUALLY run and passed. A gate whose every
    # command was missing from that machine (exit 127) is not a proof.
    proved = passed and any(r["status"] == "passed" for r in results)
    blob = {
        "origin": "derived_in_place",
        "applicable": True,
        "commands": results,
        "passed": passed,
        "proved": proved,
    }
    await _persist(
        service,
        run=run,
        blob=blob,
        outcome=VerificationOutcome.PASSED if passed else VerificationOutcome.FAILED,
    )
    logger.info(
        "inplace_gate_ran",
        run_id=str(run.id),
        commands=len(results),
        passed=passed,
        proved=proved,
    )
    return blob


async def _run_commands(
    service: VerificationService, *, gate: Any, box: SandboxSession
) -> list[dict[str, Any]]:
    """Run each derived command on the founder's machine; exit code is the verdict.

    No PATH is prepended: this box does not provision a venv (the founder's own
    toolchain is already set up — see ``ensure_sandbox_ready``), so the commands
    run in their own environment, which is the one they are meant to run in.
    """
    from backend.config import get_settings  # noqa: PLC0415

    timeout_s = get_settings().verify_gate_command_timeout_s
    results: list[dict[str, Any]] = []
    for c in gate.commands:
        res = await box.exec(c.command, timeout_s=timeout_s, shell=True)
        if res.exit_code == 0 and not res.timed_out:
            status = "passed"
        elif res.exit_code == 127:
            # The tool is not on that machine — recorded, never a false-fail.
            status = "unavailable"
        else:
            status = "failed"
        results.append(
            {
                "command": c.command,
                "kind": c.kind,
                "status": status,
                "exit_code": res.exit_code,
                "timed_out": res.timed_out,
                "output": "\n".join(o for o in (res.stdout, res.stderr) if o)[-2000:],
            }
        )
    return results


async def _persist(
    service: VerificationService,
    *,
    run: Any,
    blob: dict[str, Any],
    outcome: VerificationOutcome,
) -> None:
    """Record the gate so a PROVED claim is inspectable, not asserted."""
    import uuid as _uuid  # noqa: PLC0415

    service._session.add(
        VerificationResult(
            id=_uuid.uuid4(),
            run_id=run.id,
            work_step_id=None,
            workspace_id=run.workspace_id,
            outcome=outcome,
            contract=VerificationContract(checks=()).to_dict(),
            result={
                "derived_gate": blob,
                "execution_target": "client_attach",
                # The gate was EXPECTED (a manifest exists on that machine) —
                # what makes a non-passing outcome a real failure, not an absence.
                "gate_expected": True,
            },
        )
    )


__all__ = ["run_inplace_gate"]
