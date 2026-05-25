"""VerificationService — the reusable verify machinery (Workflow §1.2).

Lifted out of :class:`~backend.execution.orchestrator.RunOrchestrator` so
BOTH the native compute loop (api-llm path) and the executor orchestrator
(CLI-worker path) run the *same* verification: one assembled contract
(work-declared + BSage canonical retrieval), command checks run in the
sandbox, judge checks graded by an LLM, and the :class:`VerificationResult`
persisted with a "verify" activity.

This service only *runs* verification and returns the
:class:`VerificationResult`. The PASS gate consumers — setting
``proof_state=PROVED`` on a passing verdict, the no-contract human-review
Decision, the FAIL→replan branch — stay with the orchestrators that own the
loop. Behaviour here is identical to the inline machinery it replaces.

Dependencies are explicit (constructor): ``session`` (persist the result +
activity), ``llm`` (the completion seam used for the LLM-judge), and an
optional ``retriever`` (BSage canon). The ``llm`` seam is the same
:class:`~backend.execution.orchestrator.LoopLlm` Protocol the loop injects —
declared here locally as :class:`JudgeLlm` to keep the service free of an
orchestrator import (avoiding an import cycle); any ``LoopLlm`` satisfies it
structurally.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import (
    ExecutionRun,
    ExecutionRunActivity,
    RunAttempt,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
)
from backend.execution.verifier.contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
)
from backend.supervisor.sandbox import SandboxError, SandboxSession

logger = structlog.get_logger(__name__)

# Per-command verify timeout + the byte cap on each file fed to the judge.
# These are the canonical home (shared by both orchestrators); the native
# orchestrator re-imports them for back-compat.
VERIFY_TIMEOUT_S = 60.0
_JUDGE_FILE_CONTEXT_BYTES = 8 * 1024


@runtime_checkable
class JudgeLlm(Protocol):
    """The completion seam the LLM-judge uses. Structurally identical to
    :class:`~backend.execution.orchestrator.LoopLlm` (``tools=None`` → plain
    completion) — declared locally so the service does not import the
    orchestrator. The returned object only needs a ``content: str`` field."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> Any: ...


@runtime_checkable
class CanonRetriever(Protocol):
    """Read-only BSage retrieval seam (Workflow §1.2). Given the signals of
    the change (changed paths + the work summary), returns canonical pattern
    statements to fold into the verify contract as judge criteria."""

    async def retrieve_for_signals(self, signals: str) -> list[str]: ...


class VerificationService:
    """Runs one assembled verify contract and persists its result.

    Stateless across calls apart from its injected dependencies; the same
    instance can verify many runs."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        llm: JudgeLlm,
        retriever: CanonRetriever | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._retriever = retriever

    async def assemble_contract(
        self,
        *,
        declared_contract: Any | None,
        written_paths: list[str],
        final_text: str,
    ) -> VerificationContract | None:
        """Merge the agent's declared contract (raw/parsed dict, or ``None``
        for non-native callers) with BSage canonical patterns retrieved for
        the change. Returns ``None`` when no usable check remains (→ the
        caller routes to a human-review Decision; never a silent pass)."""
        declared = (
            parse_verification_contract(declared_contract)
            if declared_contract is not None
            else None
        )
        checks: list[VerificationCheck] = list(declared.checks) if declared is not None else []

        if self._retriever is not None:
            signals = (final_text + "\n" + "\n".join(written_paths)).strip()
            patterns = [
                p.strip() for p in await self._retriever.retrieve_for_signals(signals) if p.strip()
            ]
            if patterns:
                checks.append(
                    VerificationCheck(
                        kind="judge",
                        criteria=tuple(patterns),
                        rationale="BSage canonical patterns retrieved for this change",
                    )
                )

        if not checks:
            return None
        return VerificationContract(checks=tuple(checks))

    async def verify(
        self,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        contract: VerificationContract,
        box: SandboxSession,
        written_paths: list[str],
        final_text: str,
    ) -> VerificationResult:
        """Run the contract's command + judge checks, persist a
        :class:`VerificationResult` (PASS = all commands pass AND the judge
        passes), record a "verify" activity, and return the result."""
        command_results = await self._run_command_checks(contract, box)
        all_cmd_pass = all(r["passed"] for r in command_results)

        judge_blob: dict[str, Any] | None = None
        judge_pass = True
        criteria = [c for chk in contract.judge_checks for c in chk.criteria]
        if criteria:
            judge_blob = await self._run_judge(criteria, written_paths, final_text, box)
            judge_pass = bool(judge_blob.get("passed"))

        passed = all_cmd_pass and judge_pass
        outcome = VerificationOutcome.PASSED if passed else VerificationOutcome.FAILED
        vr = VerificationResult(
            id=uuid.uuid4(),
            run_id=run.id,
            work_step_id=work_step.id,
            workspace_id=run.workspace_id,
            outcome=outcome,
            contract=contract.to_dict(),
            result={"command_results": command_results, "judge": judge_blob},
        )
        self._session.add(vr)
        self._session.add(
            ExecutionRunActivity(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                activity_type="verify",
                payload={
                    "attempt_id": str(attempt.id),
                    "outcome": outcome.value,
                    "commands": len(command_results),
                },
            )
        )
        await self._session.flush()
        return vr

    async def _run_command_checks(
        self, contract: VerificationContract, box: SandboxSession
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for check in contract.command_checks:
            command = check.command or ""
            res = await box.exec(command, timeout_s=VERIFY_TIMEOUT_S, shell=True)
            output = "\n".join(c for c in (res.stdout, res.stderr) if c)[-2000:]
            results.append(
                {
                    "command": command,
                    "exit_code": res.exit_code,
                    "timed_out": res.timed_out,
                    "passed": res.exit_code == 0 and not res.timed_out,
                    "output": output,
                }
            )
        return results

    async def _run_judge(
        self,
        criteria: list[str],
        written_paths: list[str],
        final_text: str,
        box: SandboxSession,
    ) -> dict[str, Any]:
        file_blobs: list[str] = []
        for path in written_paths[:5]:
            try:
                data = await box.read_file(path, _JUDGE_FILE_CONTEXT_BYTES)
            except SandboxError:
                continue
            file_blobs.append(f"--- {path} ---\n{data.decode('utf-8', errors='replace')}")
        criteria_block = "\n".join(f"- {c}" for c in criteria)
        work_block = ("\n\n".join(file_blobs))[:12000] or "(no file content captured)"
        judge_messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict verification judge. Decide whether the produced work "
                    "satisfies EVERY criterion. Respond with ONLY a JSON object: "
                    '{"passed": <true|false>, "reasoning": "<short>"}.'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Criteria:\n{criteria_block}\n\n"
                    f"Work summary: {final_text or '(none)'}\n\n"
                    f"Changed files:\n{work_block}"
                ),
            },
        ]
        turn = await self._llm.complete(messages=judge_messages, tools=None)
        return parse_judge_verdict(turn.content)


def parse_judge_verdict(raw: str) -> dict[str, Any]:
    """Tolerant parse of the judge LLM's JSON verdict. A failure to parse is
    treated as a non-pass (never a silent pass)."""
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"passed": False, "reasoning": "unparseable judge response", "raw": raw[:500]}
    if not isinstance(data, dict):
        return {"passed": False, "reasoning": "judge response not an object", "raw": raw[:500]}
    return {"passed": bool(data.get("passed")), "reasoning": str(data.get("reasoning") or "")}


__all__ = [
    "VERIFY_TIMEOUT_S",
    "CanonRetriever",
    "JudgeLlm",
    "VerificationService",
    "parse_judge_verdict",
]
