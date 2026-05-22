"""RunOrchestrator — the agent compute loop.

Workflow §11.3 (agent-loop iteration: plan → act → verify → branch) +
§1.2 (verify = one assembled contract: work-declared + BSage retrieval).

This module owns the *compute* lifecycle of an :class:`ExecutionRun`:
the iterative ``plan → act → verify`` loop that drives a work LLM through
a sandboxed :class:`~backend.execution.tools.ToolRegistry`, assembles a
:class:`~backend.execution.verifier.contract.VerificationContract` from
what the work LLM declared plus (optionally) BSage canonical retrieval,
runs the contract's command checks in the sandbox + judge checks via an
LLM, and lands a verdict.

:class:`~backend.orchestrator.agent_runner.AgentRunner` owns the
*transactional* lifecycle (the ``open → running → review_ready`` status
flow) and delegates the compute to this module — exactly as its docstring
already intends.

Terminal loop outcomes (Workflow §1 ε / §6 — there is no ``abandoned``):

* ``verified`` — work complete + contract passed.
* ``needs_decision`` — the loop is stuck or the LLM asked the founder a
  question; a :class:`~backend.execution.db.Decision` row is created and
  the run pauses (not a DB-terminal — resolution re-enters the loop).
* ``system_error`` — the process/infra failed (sandbox unavailable, an
  unexpected exception). Rare.

The loop depends on ONE LLM seam — the :class:`LoopLlm` Protocol — so the
caller can inject the production gateway adapter
(:class:`~backend.execution.loop_llm.GatewayLoopLlm`) or a deterministic
test stub. (Per the ``bsvibe-llm-wrapper-not-raw-litellm`` rule: one
Protocol, never a Union of concretes.)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.execution.db import (
    Decision,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.tools import ToolError, ToolRegistry
from backend.execution.verifier.contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
)
from backend.supervisor.sandbox import SandboxError, SandboxManager, SandboxSession

logger = structlog.get_logger(__name__)

LoopOutcome = Literal["verified", "needs_decision", "system_error"]

VERIFY_TIMEOUT_S = 60.0
MAX_NO_WORK_NUDGES = 2
_JUDGE_FILE_CONTEXT_BYTES = 8 * 1024


@dataclass(frozen=True)
class LoopToolCall:
    """One tool call the work LLM emitted in a turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LoopTurn:
    """The work LLM's response for one plan turn — free text plus any
    tool calls. ``tool_calls`` empty means the model produced only prose
    (a signal that it believes the step is done)."""

    content: str
    tool_calls: tuple[LoopToolCall, ...] = ()


@runtime_checkable
class LoopLlm(Protocol):
    """The single LLM dispatch seam the loop depends on.

    ``tools`` is the OpenAI-style tool schema slice; ``None`` means a
    plain completion (used for the LLM-judge verify call). Returns a
    :class:`LoopTurn`."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn: ...


@runtime_checkable
class CanonRetriever(Protocol):
    """Read-only BSage retrieval seam (Workflow §1.2). Given the signals
    of the change (changed paths + the work summary), returns canonical
    pattern statements to fold into the verify contract as judge criteria.
    """

    async def retrieve_for_signals(self, signals: str) -> list[str]: ...


@dataclass
class LoopResult:
    """Outcome of one :meth:`RunOrchestrator.run` invocation."""

    outcome: LoopOutcome
    run_id: uuid.UUID
    work_step_id: uuid.UUID | None = None
    run_attempt_id: uuid.UUID | None = None
    verification_result_id: uuid.UUID | None = None
    decision_id: uuid.UUID | None = None
    written_paths: list[str] = field(default_factory=list)
    summary: str = ""


# Tools the work LLM may use during the loop. ``ask_user_question`` is a
# loop-owned pseudo-tool (not in the shared ToolRegistry) handled inline.
WORK_TOOLS: tuple[str, ...] = (
    "file_read",
    "file_list",
    "file_write",
    "file_edit",
    "shell_exec",
    "declare_verification",
)

ASK_USER_QUESTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user_question",
        "description": (
            "Pause the run and ask the founder a blocking question when you "
            "genuinely cannot proceed without a human decision. This creates a "
            "Decision and stops the loop until it is resolved — use it only when "
            "no tool call can unblock you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The blocking question for the founder.",
                },
                "context": {
                    "type": "string",
                    "description": "Why you are blocked / what you have tried.",
                },
            },
            "required": ["question"],
        },
    },
}

_SYSTEM_PROMPT = (
    "You are an autonomous engineer working inside a sandboxed workspace. "
    "Use the tools to inspect and change files. Before writing code, call "
    "declare_verification to commit to how the work will be checked (prefer a "
    "command check that runs the real test/lint, scoped to the files you "
    "changed). When the step is complete, stop calling tools and reply with a "
    "short plain-text summary — that triggers verification. If you are blocked "
    "on a decision only the founder can make, call ask_user_question."
)


class RunOrchestrator:
    """Drives the plan → act → verify → iterate loop for one run."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        llm: LoopLlm,
        sandbox_manager: SandboxManager,
        max_cycles: int | None = None,
        retriever: CanonRetriever | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._sandbox_manager = sandbox_manager
        # Round cap: explicit override wins; otherwise the env-overridable
        # Settings knob (promoted in Round 9). Defaults tuned for local LLMs.
        self._max_cycles = (
            max_cycles if max_cycles is not None else get_settings().execution_work_round_budget
        )
        self._retriever = retriever

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        project_id = run.product_id or run.id
        work_step = WorkStep(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            title=_intent_title(run),
            status=WorkStepStatus.RUNNING,
            proof_state=ProofState.UNTESTED,
            payload={},
        )
        attempt = RunAttempt(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            phase=RunAttemptPhase.PLANNING,
            payload={},
        )
        self._session.add_all([work_step, attempt])
        await self._session.flush()

        try:
            box = await self._sandbox_manager.acquire(project_id, str(workspace_dir))
        except Exception as exc:  # noqa: BLE001 — infra failure → system_error
            await self._record(run, attempt, "error", {"stage": "acquire", "error": str(exc)})
            attempt.phase = RunAttemptPhase.FAILED
            work_step.status = WorkStepStatus.FAILED
            await self._session.flush()
            logger.warning(
                "run_orchestrator_sandbox_unavailable", run_id=str(run.id), error=str(exc)
            )
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"sandbox unavailable: {exc}",
            )

        try:
            return await self._drive_loop(
                run=run,
                work_step=work_step,
                attempt=attempt,
                box=box,
                workspace_dir=workspace_dir,
            )
        except Exception as exc:  # noqa: BLE001 — any loop crash → system_error, never leak
            await self._record(run, attempt, "error", {"stage": "loop", "error": str(exc)})
            attempt.phase = RunAttemptPhase.FAILED
            work_step.status = WorkStepStatus.FAILED
            await self._session.flush()
            logger.exception("run_orchestrator_loop_crash", run_id=str(run.id))
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"loop crashed: {exc}",
            )
        finally:
            await self._sandbox_manager.release(project_id)

    async def _drive_loop(
        self,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        box: SandboxSession,
        workspace_dir: Path,
    ) -> LoopResult:
        registry = ToolRegistry(workspace_dir=workspace_dir, sandbox=box)
        tools_schema = [*registry.schema_for(list(WORK_TOOLS)), ASK_USER_QUESTION_TOOL]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _intent_title(run)},
        ]
        attempt.phase = RunAttemptPhase.WORKING
        await self._session.flush()

        written_paths: list[str] = []
        final_text = ""
        no_work_nudges = 0

        for _cycle in range(self._max_cycles):
            turn = await self._llm.complete(messages=messages, tools=tools_schema)
            final_text = turn.content or final_text
            await self._record(
                run,
                attempt,
                "llm_turn",
                {"content": turn.content[:500], "tool_calls": [c.name for c in turn.tool_calls]},
            )

            ask = next((c for c in turn.tool_calls if c.name == "ask_user_question"), None)
            if ask is not None:
                decision = await self._create_decision(
                    run,
                    work_step,
                    kind="ask_user_question",
                    payload={
                        "question": str(ask.arguments.get("question") or ""),
                        "context": str(ask.arguments.get("context") or ""),
                    },
                    rationale="work LLM asked the founder a blocking question",
                )
                return self._decision_result(
                    run, work_step, attempt, decision, written_paths, final_text
                )

            if turn.tool_calls:
                messages.append(_assistant_tool_call_message(turn.content, turn.tool_calls))
                for call in turn.tool_calls:
                    output, ok, writes = await _invoke_tool_safely(
                        registry, call.name, call.arguments
                    )
                    if ok:
                        for path in writes:
                            if path not in written_paths:
                                written_paths.append(path)
                    await self._record(
                        run,
                        attempt,
                        "tool_call",
                        {"tool": call.name, "ok": ok, "writes": writes},
                    )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                continue

            # No tool calls: the model believes the step is done.
            if (
                not written_paths
                and registry.declared_contract is None
                and no_work_nudges < MAX_NO_WORK_NUDGES
            ):
                no_work_nudges += 1
                messages.append({"role": "assistant", "content": turn.content or "(no tool calls)"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have not changed any file or declared a verification contract yet. "
                            "A prose answer is not a deliverable. Use the tools to do the work, then "
                            "declare_verification, then summarise."
                        ),
                    }
                )
                continue

            attempt.phase = RunAttemptPhase.VERIFYING
            await self._session.flush()
            contract = await self._assemble_contract(registry, written_paths, final_text)
            if contract is None:
                # No usable check → never a silent pass (contract.py philosophy).
                decision = await self._create_decision(
                    run,
                    work_step,
                    kind="human_review_required",
                    payload={"reason": "no_verification_declared", "written_paths": written_paths},
                    rationale="work finished without any verifiable contract",
                )
                return self._decision_result(
                    run, work_step, attempt, decision, written_paths, final_text
                )

            verdict = await self._verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=box,
                written_paths=written_paths,
                final_text=final_text,
            )
            if verdict.outcome is VerificationOutcome.PASSED:
                return await self._finish_verified(
                    run, work_step, attempt, written_paths, final_text, verdict
                )
            # Failed: feed the verifier output back and re-plan on the next cycle.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Verification FAILED. Details:\n"
                        f"{json.dumps(verdict.result)[:1500]}\n"
                        "Fix the problem and try again, then send your summary."
                    ),
                }
            )

        # Cycle cap reached without a passing verdict → stuck → Decision (§6).
        decision = await self._create_decision(
            run,
            work_step,
            kind="verification_failed",
            payload={"reason": "round_cap_reached", "written_paths": written_paths},
            rationale="agent loop exhausted its round budget without a passing verification",
        )
        return self._decision_result(run, work_step, attempt, decision, written_paths, final_text)

    # -- verify ------------------------------------------------------------

    async def _verify(
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
        await self._record(
            run, attempt, "verify", {"outcome": outcome.value, "commands": len(command_results)}
        )
        await self._session.flush()
        return vr

    async def _assemble_contract(
        self, registry: ToolRegistry, written_paths: list[str], final_text: str
    ) -> VerificationContract | None:
        declared = (
            parse_verification_contract(registry.declared_contract)
            if registry.declared_contract is not None
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
        return _parse_judge_verdict(turn.content)

    # -- terminal helpers --------------------------------------------------

    async def _finish_verified(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        written_paths: list[str],
        final_text: str,
        verdict: VerificationResult,
    ) -> LoopResult:
        work_step.status = WorkStepStatus.VERIFIED
        work_step.proof_state = ProofState.PROVED
        attempt.phase = RunAttemptPhase.COMPLETED
        attempt.finished_at = _utcnow()

        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            deliverable_type=DeliverableType.CODE,
            artifact_uri=None,
            diff_url=None,
            payload={"artifact_refs": written_paths, "summary": final_text},
        )
        self._session.add(deliverable)
        await self._session.flush()

        # Deliver event — drained by the DeliveryWorker (delivery_events table).
        await self._emit_deliver_event(run, deliverable, written_paths, final_text)
        # Settle observation — the run-trace/observation side channel (§1).
        await self._record(
            run,
            attempt,
            "settle",
            {"verified": True, "artifact_refs": written_paths, "summary": final_text[:500]},
        )
        await self._session.flush()
        logger.info(
            "run_orchestrator_verified",
            run_id=str(run.id),
            artifact_refs=written_paths,
        )
        return LoopResult(
            outcome="verified",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            verification_result_id=verdict.id,
            written_paths=written_paths,
            summary=final_text,
        )

    async def _emit_deliver_event(
        self,
        run: ExecutionRun,
        deliverable: Deliverable,
        written_paths: list[str],
        final_text: str,
    ) -> None:
        from backend.delivery.db import DeliveryEventRow  # noqa: PLC0415 — cross-domain, local

        self._session.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=run.workspace_id,
                deliverable_id=deliverable.id,
                artifact_type=DeliverableType.CODE.value,
                payload={"artifact_refs": written_paths, "summary": final_text[:500]},
            )
        )

    async def _create_decision(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str,
    ) -> Decision:
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            decision=kind,
            actor_id=None,
            rationale=rationale,
            payload=payload,
        )
        self._session.add(decision)
        await self._session.flush()
        logger.info("run_orchestrator_needs_decision", run_id=str(run.id), kind=kind)
        return decision

    def _decision_result(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        decision: Decision,
        written_paths: list[str],
        final_text: str,
    ) -> LoopResult:
        return LoopResult(
            outcome="needs_decision",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            decision_id=decision.id,
            written_paths=written_paths,
            summary=final_text,
        )

    async def _record(
        self,
        run: ExecutionRun,
        attempt: RunAttempt,
        activity_type: str,
        payload: dict[str, Any],
    ) -> None:
        self._session.add(
            ExecutionRunActivity(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                activity_type=activity_type,
                payload={"attempt_id": str(attempt.id), **payload},
            )
        )


async def _invoke_tool_safely(
    registry: ToolRegistry, name: str, arguments: dict[str, Any]
) -> tuple[str, bool, list[str]]:
    """Run ``registry.invoke`` and translate failures into a string the
    LLM can read. Returns (output, ok, write_paths)."""
    writes: list[str] = []
    if name in ("file_write", "file_edit"):
        path = arguments.get("path")
        if isinstance(path, str):
            writes.append(path)
    try:
        output = await registry.invoke(name, arguments)
        return output, True, writes
    except ToolError as exc:
        return f"ERROR: {exc}", False, writes


def _assistant_tool_call_message(
    content: str, tool_calls: tuple[LoopToolCall, ...]
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in tool_calls
        ],
    }


def _parse_judge_verdict(raw: str) -> dict[str, Any]:
    """Tolerant parse of the judge LLM's JSON verdict. A failure to parse
    is treated as a non-pass (never a silent pass)."""
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


def _intent_title(run: ExecutionRun) -> str:
    payload = run.payload or {}
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:512]


def _utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "WORK_TOOLS",
    "CanonRetriever",
    "LoopLlm",
    "LoopOutcome",
    "LoopResult",
    "LoopToolCall",
    "LoopTurn",
    "RunOrchestrator",
]
