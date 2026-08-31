"""The plan→act→verify→branch cycle body — extracted from RunOrchestrator.

Lifted from ``backend.execution.orchestrator._drive_loop`` (Lift H2a
sub-split / v8 §17.1). The cycle body is the longest single block in
the loop file; pulling it into a free function keeps ``agent_loop.py``
under the 600 LOC ceiling without changing semantics. The function takes
the orchestrator as its first argument (a thin protocol-like contract
that maps 1-1 to the methods the old cycle body called on ``self``) so
the cycle continues to use the orchestrator's persistence + verify +
audit delegations exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.workflow.application._loop_context import (
    _consume_merge_conflict,
    _initial_user_message,
    _intent_title,
    _merge_conflict_directive,
    _resumption_messages,
    is_client_attach_run,
    settle_client_attach,
    system_prompt_for,
)
from backend.workflow.application.audit_events import (
    DecisionPending,
    LlmTurn,
    LoopTerminal,
    ToolCall,
    VerifyRun,
)
from backend.workflow.application.inplace_gate import capture_inplace_baseline
from backend.workflow.application.tool_registry import (
    ASK_USER_QUESTION_TOOL,
    MAX_NO_WORK_NUDGES,
    RUN_TOOL_INNER_NAMES,
    WORK_TOOL_STATE_KEY,
    _assistant_tool_call_message,
    _invoke_tool_safely,
    _sanitize_ask_user_question_options,
    assemble_run_tool_registry,
)
from backend.workflow.application.undeclared_verification import (
    settle_undeclared_verification,
)
from backend.workflow.domain.emit_deliverable import (
    EMIT_DELIVERABLE_NAME,
    EMIT_DELIVERABLE_TOOL,
    _safe_args,
    handle_emit_deliverable,
)
from backend.workflow.domain.verification_feedback import render_verification_failure
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunAttempt,
    RunAttemptPhase,
    VerificationOutcome,
    WorkStep,
)
from backend.workflow.infrastructure.sandbox import SandboxSession

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, RunOrchestrator


async def _pending_question(session: Any, run: ExecutionRun) -> Decision | None:
    """A question the agent asked the founder OUT OF BAND — i.e. not via a tool call the loop
    could see, but by calling the MCP work tool, which records the Decision server-side.

    That is the only way an executor's CLI can ask anything (parity audit #20: no tool call
    other than ``declare_verification`` can come back from an executor), so without this the
    run would keep working while the founder's question sat pending, unanswered."""
    from sqlalchemy import select  # noqa: PLC0415

    rows = await session.execute(
        select(Decision).where(
            Decision.run_id == run.id,
            Decision.decision == "ask_user_question",
            Decision.status == DecisionStatus.PENDING,
        )
    )
    return rows.scalars().first()  # type: ignore[no-any-return]


async def _remote_work_state(orch: RunOrchestrator, run: ExecutionRun) -> dict[str, Any] | None:
    """The work-tool state the MCP transport committed for this run — read FRESH.

    The MCP handlers run in the API process and commit there, so the loop's own ORM copy of the
    run never learns about it. Select the column rather than refreshing the instance: the loop
    mutates ``run`` itself, and a refresh would clobber its pending changes.
    """
    from sqlalchemy import select  # noqa: PLC0415

    rows = await orch._session.execute(
        select(ExecutionRun.payload).where(ExecutionRun.id == run.id)
    )
    payload = rows.scalar_one_or_none() or {}
    state = payload.get(WORK_TOOL_STATE_KEY)
    return state if isinstance(state, dict) else None


async def _sync_remote_tool_state(
    registry: Any, written_paths: list[str], *, state: dict[str, Any] | None
) -> None:
    """Teach the loop's registry what the agent did through the MCP work tools.

    Restores the same per-run latches the MCP transport persists — the declared contract, the
    grounding, the declared knowledge — and merges the paths the agent WROTE into the loop's
    accumulator, which becomes the verified Deliverable's ``artifact_refs``.

    Idempotent: it runs every turn, and both the registry and ``written_paths`` dedupe.
    """
    if not state:
        return
    registry.restore_state(state)
    for path in registry.written_paths:
        if path not in written_paths:
            written_paths.append(path)


async def drive_loop(  # noqa: PLR0911, PLR0912, PLR0915 — preserved cycle body
    orch: RunOrchestrator,
    *,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    box: SandboxSession,
    workspace_dir: Path,
) -> LoopResult:
    """Run the plan→act→verify→branch cycle until a terminal verdict.

    Terminal outcomes (Workflow §1 ε / §6 — there is no ``abandoned``):

    * ``verified`` — work complete + contract passed.
    * ``needs_decision`` — the loop is stuck or the LLM asked the founder a
      question; a Decision row is created and the run pauses.

    Semantics are byte-identical to the pre-H2a cycle body; only the
    physical location changed. All persistence + verify + audit work
    routes back through the orchestrator's delegations so the wiring
    (session, retriever, redis_client, live_event_bus, settings) is
    consistent with the rest of the loop.
    """
    # INV-7 #1 — the SAME factory the MCP transport calls, so the base tools + knowledge_search
    # cannot drift between the two paths. invoke_skill + connector actions are layered on AFTER:
    # their registration code lives in backend.extensions / backend.connectors, which the MCP
    # context is forbidden to import, so they ride the worker path only (see RUN_TOOL_INNER_NAMES).
    registry = assemble_run_tool_registry(
        workspace_dir=workspace_dir,
        sandbox=box,
        retriever=orch._retriever,
        # A-2 — 선언 시점 패턴 조회의 신호 절반. 나머지 절반(만진 파일)은 레지스트리가 안다.
        intent_text=_intent_title(run),
    )
    extra_tool_names = orch._register_invoke_skill_tool(registry)
    connector_tool_names = await orch._register_connector_action_tools(
        registry, run=run, work_step=work_step
    )
    tools_schema = [
        *registry.schema_for([*RUN_TOOL_INNER_NAMES, *extra_tool_names, *connector_tool_names]),
        ASK_USER_QUESTION_TOOL,
        # B12a — mid-loop Deliver events: one per external artifact emitted
        # DURING the run, BEFORE the verified terminal.
        EMIT_DELIVERABLE_TOOL,
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt_for(run)},
        _initial_user_message(run),
    ]
    # B6 — seed canon relevant to the run intent.
    seed = await orch._knowledge_seed_message(run)
    if seed is not None:
        messages.append(seed)
    # A later step of a split request: seed what the prior step produced. The
    # step's OWN brief is its ``intent_text``, so nothing is injected on top.
    if (prior_step := orch._prior_step_message(run)) is not None:
        messages.append(prior_step)
    # B9a — frame-matched skill hint.
    if (skill_hint := orch._suggested_skill_message()) is not None:
        messages.append(skill_hint)
    # Resumption context (founder-resolved prior questions).
    messages.extend(_resumption_messages(run))
    # PR7 — a re-dispatched merge conflict: surface the conflict to the agent
    # BEFORE its turn, then consume the one-shot key so a later resume does not
    # re-inject it (a persistent marker classifies any ask raised now as a
    # merge_conflict_review Decision).
    conflict_directive = _merge_conflict_directive(run)
    if conflict_directive is not None:
        messages.append(conflict_directive)
        _consume_merge_conflict(run)
    attempt.phase = RunAttemptPhase.WORKING
    await orch._session.flush()

    written_paths: list[str] = []
    final_text = ""
    no_work_nudges = 0
    # #692 — a client_attach run works in the founder's own directory on the worker
    # (through BSVibe's tools, backed by ``ClientWorkerSandboxSession``), so the
    # server holds no copy of the source and settles on the in-place gate rather
    # than on a server-side diff. Resolved once here; consumed at the "model is
    # done" branch below.
    client_attach = await is_client_attach_run(orch._session, run)
    # Where the tree stood BEFORE the agent touched it — knowable only here, since
    # the agent's commits then move HEAD. Two consumers: the in-place gate, and the
    # JUDGE (which read ``HEAD~1 HEAD`` — only the agent's LAST commit — so work
    # built in an earlier turn was invisible and got rejected as unverifiable).
    # Captured for EVERY run: a sandbox clone's HEAD is equally unrecoverable.
    # COMMIT FIRST — ``flush()`` and the read above leave a transaction open, and
    # this is an EXTERNAL sandbox call; holding a pooled connection across it is
    # the #632/#686/#680 outage shape (proven by test_session_release.py's
    # 1-connection parking test).
    await orch._session.commit()
    run_baseline = await capture_inplace_baseline(box)
    inplace_baseline = run_baseline if getattr(box, "runs_in_place", False) else None

    for _cycle in range(orch._max_cycles):
        # Cooperative cancel — stop at the turn boundary if the run was cancelled
        # mid-flight, instead of dispatching another (expensive) LLM/executor
        # turn and burning the round budget. The transition-time guard alone let
        # a cancelled run keep turning to exhaustion (dogfood dd2bd3a3).
        if await orch._run_cancelled(run):
            await orch._audit(run, attempt, LoopTerminal, {"outcome": "cancelled", "cycle": _cycle})
            return orch._cancelled_result(run, work_step, attempt, written_paths, final_text)
        # Drive-session-release (B) — release the pooled DB connection for the
        # duration of the (up to 30-minute) executor turn. Committing at the turn
        # boundary ends the orchestrator session's open transaction, so NO
        # connection is held idle-in-transaction across the ``complete()`` await
        # — the pool-exhaustion outage this refactor fixes. The engine uses
        # ``expire_on_commit=False`` (see runtime/lifecycle.py) so every loaded
        # ORM attribute survives the commit; post-turn writes autobegin a fresh
        # short transaction. Nothing between this commit and ``complete()``
        # touches the DB. ``claimed_at`` is refreshed here as a heartbeat so a
        # legitimately long multi-turn drive is never mistaken for a stale claim
        # and reaped by ``AgentWorker`` (the lease is 2× the executor timeout, so
        # a single turn is always safe; the heartbeat covers many turns).
        run.claimed_at = datetime.now(UTC)
        await orch._session.commit()
        turn = await orch._llm.complete(messages=messages, tools=tools_schema)
        final_text = turn.content or final_text
        # An EXECUTOR agent acts through the MCP work tools, which run in the API process —
        # its calls never pass through this loop's ``_invoke_tool_safely``, the only place the
        # native path learns what was written. Without this the loop ends the run believing the
        # agent did nothing: empty ``artifact_refs`` (the PR's changed-file list, the settle
        # tags, the proof view's file whitelist), a summary that falls back to the model's raw
        # narration, and a verify-first gate that looks undeclared.
        #
        # The registry already exports that state and the MCP transport already persists it on
        # the run. The loop simply never read it back. A LiteLLM run persists none: no-op.
        await _sync_remote_tool_state(
            registry,
            written_paths,
            state=await _remote_work_state(orch, run),
        )
        await orch._record(
            run,
            attempt,
            "llm_turn",
            {"content": turn.content[:500], "tool_calls": [c.name for c in turn.tool_calls]},
        )
        await orch._audit(
            run,
            attempt,
            LlmTurn,
            {
                "cycle": _cycle,
                "tool_calls": [c.name for c in turn.tool_calls],
                "content_len": len(turn.content or ""),
            },
        )

        # T1b — the agent may also have asked the founder OUT OF BAND: an executor's CLI
        # cannot emit an ``ask_user_question`` tool call, so it asks by calling the MCP tool,
        # which records the Decision server-side. Nothing in ``turn.tool_calls`` says so.
        # The pause is therefore owned by the SERVER: ask the run, do not trust the agent to
        # stop (a coding CLI trusts its own tools over anything the prompt says — measured).
        out_of_band = await _pending_question(orch._session, run)
        if out_of_band is not None:
            await orch._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "needs_decision", "decision_id": str(out_of_band.id)},
            )
            return orch._decision_result(
                run, work_step, attempt, out_of_band, written_paths, final_text
            )

        ask = next((c for c in turn.tool_calls if c.name == "ask_user_question"), None)
        if ask is not None:
            payload: dict[str, Any] = {
                "question": str(ask.arguments.get("question") or ""),
                "context": str(ask.arguments.get("context") or ""),
            }
            options = _sanitize_ask_user_question_options(ask.arguments.get("options"))
            if options is not None:
                payload["options"] = options
            decision = await orch._create_decision(
                run,
                work_step,
                kind="ask_user_question",
                payload=payload,
                rationale="work LLM asked the founder a blocking question",
            )
            await orch._audit(
                run,
                attempt,
                DecisionPending,
                {
                    "kind": "ask_user_question",
                    "decision_id": str(decision.id),
                    "question": payload.get("question", ""),
                },
            )
            await orch._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "needs_decision", "decision_id": str(decision.id)},
            )
            return orch._decision_result(
                run, work_step, attempt, decision, written_paths, final_text
            )

        if turn.tool_calls:
            messages.append(_assistant_tool_call_message(turn.content, turn.tool_calls))
            for call in turn.tool_calls:
                # B12a — emit_deliverable is a LOOP-owned tool (not in the
                # registry). Persists a partial Deliverable + DeliveryEventRow.
                if call.name == EMIT_DELIVERABLE_NAME:
                    output = await handle_emit_deliverable(
                        orch._session,
                        run,
                        call.arguments,
                        live_event_bus=orch._live_event_bus,
                    )
                    await orch._record(
                        run,
                        attempt,
                        "deliver_event",
                        {"tool": call.name, "args": _safe_args(call.arguments)},
                    )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                    continue
                output, ok, writes = await _invoke_tool_safely(registry, call.name, call.arguments)
                if ok:
                    for path in writes:
                        if path not in written_paths:
                            written_paths.append(path)
                await orch._record(
                    run,
                    attempt,
                    "tool_call",
                    {"tool": call.name, "ok": ok, "writes": writes},
                )
                await orch._audit(
                    run,
                    attempt,
                    ToolCall,
                    {"tool": call.name, "ok": ok, "writes_count": len(writes)},
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

            # The model called tools, so it is not done — keep looping.
            #
            # T3 — there is no longer a SYNTHETIC executor tool call to special-case here.
            # An executor turn now comes back with NO tool_calls (its real calls went to the
            # MCP work tools, server-side), so it falls straight through to the "model is done"
            # branch below — where the state this loop synced from the run (its declared
            # contract, the paths it wrote) is what proves the work happened.
            continue

        # No tool calls (or the single-shot executor just declared): the model
        # believes the step is done.
        if client_attach:
            # #692 — a client_attach run settles on its own terms: the in-place
            # gate decides, and only a failure the agent can actually fix sends
            # the cycle round again. ``None`` = keep going.
            settled = await settle_client_attach(
                orch,
                run=run,
                work_step=work_step,
                attempt=attempt,
                box=box,
                messages=messages,
                baseline=inplace_baseline,
                cycle=_cycle,
                final_text=final_text,
            )
            if settled is None:
                continue
            return settled
        if (
            not written_paths
            and registry.declared_contract is None
            # 파일을 안 고친 것과 **일을 안 한 것**은 다르다. 형님이 "조사만 하고
            # 보고해라 — 파일은 하나도 쓰지 마라" 로 스코프한 런은 파일을 안 고치는
            # 것이 정답이고, 그때 이 nudge 는 제대로 조사한 에이전트를 다그친다.
            # prod 실측(2026-08-24 · 08-25 · 08-31 ×2): 그 런들의 ``writes`` 는 전부
            # 비어 있었고, 에이전트는 정확한 답을 냈으며, 한 번은 결국 형님께 질문을
            # 올려 런이 멈췄다.
            #
            # 형님 판정 2026-08-20 — *"정말 검증할게 없어서 아무것도 안한거는 통과야"*
            # — 은 ``settle_undeclared_verification`` 에 이미 적용돼 있다. 여기가
            # 그 판정을 못 받은 두 번째 지점이었다.
            #
            # 그래도 막아야 할 것은 그대로 막힌다: 도구를 **한 번도** 안 쓰고 산문만
            # 뱉는 것. 거부당한 호출(B7 게이트·denylist)은 세지 않으므로, 선언 없이
            # 쓰려다 막힌 에이전트가 자기 실패로 면제를 사지 못한다.
            and registry.succeeded_tool_calls == 0
            and no_work_nudges < MAX_NO_WORK_NUDGES
        ):
            no_work_nudges += 1
            messages.append({"role": "assistant", "content": turn.content or "(no tool calls)"})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You have not used any tool yet — no file changed, no verification "
                        "declared, nothing read or run. A prose answer alone is not a "
                        "deliverable. Use the tools to do the work; if it changes files, "
                        "declare_verification first, then summarise."
                    ),
                }
            )
            continue

        attempt.phase = RunAttemptPhase.VERIFYING
        # Verify-session-release (#686) — the SAME treatment the executor turn
        # boundary gets above, for the same reason. What follows is minutes of
        # external work: ``_assemble_contract`` calls the LLM and ``_verify``
        # runs the contract's commands + probes in the sandbox. A ``flush`` here
        # would leave that write's transaction OPEN across all of it, and the
        # per-connection ``idle_in_transaction_session_timeout`` guard (120s,
        # #633) then kills the connection — so the run does all its work and
        # dies on the post-verify ``record_activity`` write with
        # PendingRollbackError, recording nothing. Committing ends the
        # transaction and returns the connection to the pool;
        # ``expire_on_commit=False`` keeps the loaded ORM attributes usable and
        # later writes autobegin a fresh short transaction. ``claimed_at`` is
        # refreshed as a heartbeat so a long verify is never reaped as stale.
        run.claimed_at = datetime.now(UTC)
        await orch._session.commit()
        contract = await orch._assemble_contract(registry, written_paths, final_text)
        if contract is None:
            settled = await settle_undeclared_verification(
                orch,
                run=run,
                work_step=work_step,
                attempt=attempt,
                box=box,
                baseline=run_baseline,
                written_paths=written_paths,
                final_text=final_text,
                messages=messages,
                knowledge=registry.declared_knowledge,
            )
            if settled is None:
                continue  # the agent owes a declaration — the round cap terminates
            return settled

        verdict = await orch._verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=written_paths,
            final_text=final_text,
            baseline=run_baseline,
        )
        await orch._audit(
            run,
            attempt,
            VerifyRun,
            {
                "outcome": verdict.outcome.value,
                "command_checks": len(contract.command_checks),
                "judge_checks": len(contract.judge_checks),
            },
        )
        if verdict.outcome is VerificationOutcome.PASSED:
            # v2 — thread the agent's own retrospective knowledge declaration
            # (latched on the registry by declare_verification / record_knowledge)
            # into the settle payload. None for routine work → no note.
            result = await orch._finish_verified(
                run,
                work_step,
                attempt,
                written_paths,
                final_text,
                verdict,
                registry.declared_knowledge,
            )
            await orch._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "verified", "written_paths_count": len(written_paths)},
            )
            return result
        # Failed: feed the verifier output back and re-plan on the next cycle.
        # Built from the FAILURES, never a prefix of the whole result — this was
        # ``json.dumps(verdict.result)[:1500]``, and since ``command_results``
        # (advisory) serialises before ``derived_gate`` (authoritative), the
        # 1500 characters an agent received could be — and in run 010bbdd8 were —
        # entirely its own passing commands. It read "FAILED: [everything
        # passed]" and repeated the identical failure 16 times.
        vresult = verdict.result if isinstance(verdict.result, dict) else {}
        messages.append(
            {
                "role": "user",
                "content": (
                    "Verification FAILED. Details:\n"
                    f"{render_verification_failure(vresult)}\n"
                    "Fix the problem and try again, then send your summary."
                ),
            }
        )

    # Cycle cap reached without a passing verdict → stuck → Decision (§6).
    decision = await orch._create_decision(
        run,
        work_step,
        kind="verification_failed",
        payload={"reason": "round_cap_reached", "written_paths": written_paths},
        rationale="agent loop exhausted its round budget without a passing verification",
    )
    await orch._audit(
        run,
        attempt,
        DecisionPending,
        {
            "kind": "verification_failed",
            "decision_id": str(decision.id),
            "reason": "round_cap_reached",
        },
    )
    await orch._audit(
        run,
        attempt,
        LoopTerminal,
        {"outcome": "needs_decision", "decision_id": str(decision.id)},
    )
    return orch._decision_result(run, work_step, attempt, decision, written_paths, final_text)


__all__ = ["_consume_merge_conflict", "_merge_conflict_directive", "drive_loop"]
