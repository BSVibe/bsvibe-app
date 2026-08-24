"""Loop-context helpers — prompts + initial-message assembly.

Lifted from ``backend.execution.orchestrator`` (Lift H2a sub-split / v8
§17.1). Owns the *static* loop ingredients (system prompt, design-spec
directive) and the *pre-cycle context assembly* (knowledge seed, design
seed, design directive, suggested-skill hint, resumption messages,
``knowledge_search`` handler, ``invoke_skill`` adapter).

This module is private to the agent-loop conductor — kept under the
``application/`` layer so it shares the loop's coordinate system but
isolated from the conductor file so neither breaches the 600 LOC ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from backend.config import Settings
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.tool_binding import INVOKE_SKILL_NAME, register_invoke_skill
from backend.workflow.application.agent_briefing import (
    _ASK_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
)
from backend.workflow.application.tool_registry import (
    _KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT,
    _KNOWLEDGE_SEED_MAX_RESULTS,
    make_knowledge_search_handler,
)
from backend.workflow.domain.verification_feedback import render_failed_commands
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.tools import ToolRegistry

# ``make_knowledge_search_handler`` is imported (and re-exported via ``__all__``) from the clean
# ``tool_registry`` module so the worker's existing callers keep importing it from here while the
# MCP transport — forbidden from importing this ``backend.extensions``-tainted module — registers
# the same handler through the shared factory.

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult

logger = structlog.get_logger(__name__)


def _intent_text(run: ExecutionRun) -> str:
    """The founder's stored directive for ``run`` — full, never truncated.

    Prefers ``intent_text`` (the request's stable intent), falling back to a
    raw ``text`` payload and finally a placeholder. Shared source for both the
    short label (:func:`_intent_title`) and the full prompt directive
    (:func:`_intent_directive`)."""
    payload = run.payload or {}
    return str(payload.get("intent_text") or payload.get("text") or "Untitled run")


def _intent_directive(run: ExecutionRun) -> str:
    """#690 — the WHOLE founder directive, for the coding agent's first user
    message.

    Must NOT be truncated: this is the agent's actual instruction, and slicing
    it (the old ``_intent_title`` 512 cap) silently dropped requirements past
    that point — the agent built only the half it received while lint/test
    passed on that half. The directive is already bounded upstream by the
    ``bsvibe_direct`` input limit (20000 chars), so no cap is applied here.

    When the frame split the request, the founder's text is still delivered
    WHOLE and the step's brief is added as SCOPE — never as a substitute. Both
    are needed: without the founder's words a step loses every requirement the
    framer's summary omitted; without the step's scope every run in the chain
    would try to do the whole job."""
    directive = _intent_text(run)
    payload = run.payload if isinstance(run.payload, dict) else {}
    step_intent = payload.get("step_intent")
    if not isinstance(step_intent, str) or not step_intent.strip():
        return directive
    return (
        f"{directive}\n\n"
        f"This run is ONE step of that request. Your part of it: {step_intent.strip()}"
    )


def _intent_title(run: ExecutionRun) -> str:
    """A SHORT label derived from the directive — WorkStep title, audit
    ``intent`` field, and knowledge-retrieval signal.

    Capped at 512 chars on purpose: these are labels/signals, not the prompt.
    The coding agent's instruction uses :func:`_intent_directive` (uncapped)
    so requirements are never lost — see #690."""
    return _intent_text(run)[:512]


def _resumption_messages(run: ExecutionRun) -> list[dict[str, Any]]:
    """Build loop seed messages for any founder-resolved decisions.

    ``run.payload["resolved_decisions"]`` is a list of
    ``{decision_id, question, answer}`` appended by the checkpoints resolve
    endpoint. Each becomes a user message so the work LLM continues with the
    founder's answer in context instead of re-asking the blocking question."""
    payload = run.payload or {}
    resolved = payload.get("resolved_decisions") if isinstance(payload, dict) else None
    if not isinstance(resolved, list):
        return []
    messages: list[dict[str, Any]] = []
    for entry in resolved:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if not answer:
            continue
        messages.append(
            {
                "role": "user",
                "content": (
                    "The founder resolved a prior question — "
                    f"Q: {question} A: {answer}. "
                    "Continue the work with this decision."
                ),
            }
        )
    return messages


class _RetrieverSearcher:
    """Adapt a CanonRetriever to the skill runner's ``Searcher``.

    The skill runner primes a skill's system prompt via ``search(query, *,
    top_k, max_chars) -> str``; the retriever speaks ``retrieve_for_signals
    (signals) -> list[str]``. This thin adapter joins the canonical statements
    into the formatted-string shape the runner expects, capped at ``max_chars``,
    and degrades to an empty string when there is no knowledge (never raises —
    matching the retriever's own graceful-empty contract)."""

    def __init__(self, retriever: Any) -> None:
        self._retriever = retriever

    async def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        max_chars: int = 50_000,
    ) -> str:
        try:
            statements = await self._retriever.retrieve_for_signals(query)
        except Exception:  # noqa: BLE001 — priming must never crash a skill run
            logger.warning("skill_searcher_retrieve_failed", exc_info=True)
            return ""
        cleaned = [s.strip() for s in statements if s and s.strip()][:top_k]
        if not cleaned:
            return ""
        return "\n".join(f"- {s}" for s in cleaned)[:max_chars]


async def knowledge_seed_message(run: ExecutionRun, *, retriever: Any) -> dict[str, Any] | None:
    """B6 — build the loop-start knowledge seed for ``run``, or ``None``.

    Retrieves canon relevant to the run's STABLE intent (the same text the
    first user turn uses — never written_paths, none exist yet) and folds the
    top statements into a single context message so the work is informed by
    the workspace's established patterns BEFORE the act/verify cycle. No
    retriever / no patterns → ``None`` (inject nothing; an empty-knowledge
    workspace stays byte-identical to pre-B6). Never raises — a retrieval
    hiccup degrades to no seed, exactly like the B3 verify fold."""
    if retriever is None:
        return None
    signals = _intent_title(run)
    try:
        statements = await retriever.retrieve_for_signals(signals)
    except Exception:  # noqa: BLE001 — seeding must never crash the loop
        logger.warning("knowledge_seed_retrieve_failed", run_id=str(run.id), exc_info=True)
        return None
    cleaned = [
        s.strip()[:_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT] for s in statements if s and s.strip()
    ][:_KNOWLEDGE_SEED_MAX_RESULTS]
    if not cleaned:
        return None
    body = "\n".join(f"- {s}" for s in cleaned)
    logger.info("knowledge_seeded", run_id=str(run.id), count=len(cleaned))
    return {
        "role": "system",
        "content": (
            "Relevant established patterns for this workspace (consider them as you work):\n" + body
        ),
    }


def _is_ask(run: ExecutionRun) -> bool:
    """True when the frame judged this run an ASK (a question), not a PRODUCE.

    Reads the same ``path_classification`` the frame already records — this adds
    no second source and no new prediction. Tolerant of an odd payload."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    raw_frame = payload.get("frame")
    frame = raw_frame if isinstance(raw_frame, dict) else {}
    return frame.get("path_classification") == "knowledge_only"


def system_prompt_for(run: ExecutionRun) -> str:
    """The identity this run works under — investigator for an ASK, engineer
    otherwise.

    Selecting the prompt (rather than appending a retraction to it) is what
    #778 got wrong: prod ``fae09a47`` kept the engineer identity, received the
    "do not change the product" directive, and edited four files anyway. A run
    with no frame keeps the engineer prompt — a missing classification must
    never silently downgrade a build into an investigation."""
    return _ASK_SYSTEM_PROMPT if _is_ask(run) else _SYSTEM_PROMPT


def prior_step_message(run: ExecutionRun, *, settings: Settings) -> dict[str, Any] | None:
    """Fold the PRIOR step's output into the loop-start context when this run is
    a later step of a split request.

    ``None`` for an unsplit run / the first step (no prior refs) or when no
    content is readable — best-effort, never raises into the loop.

    Note what is NOT here any more: the design stage used to also get an
    injected "write a spec, do NOT implement" system message on top of the
    engineer identity. That is the exact shape #778 measured as ineffective for
    ASK runs — an identity is not undone by a later sentence — and #770 measured
    its cost here: the spec landed, nothing implemented it, and the run reported
    done. A step's brief now travels as its own ``intent_text``."""
    from backend.workflow.application.handoff import read_prior_step_context  # noqa: PLC0415

    content = read_prior_step_context(run, settings)
    if content is None:
        return None
    logger.info("prior_step_context_seeded", run_id=str(run.id))
    return {"role": "system", "content": content}


def suggested_skill_message(
    *, suggested_skill: str | None, suggested_skill_description: str | None
) -> dict[str, Any] | None:
    """B9a — the frame-matched skill hint for the loop's initial context.

    ``None`` when the frame matched no skill (the hint is omitted, loop
    unchanged). The message names the skill + its description and points the
    work LLM at ``invoke_skill`` — a hint, not a forced first action."""
    if not suggested_skill:
        return None
    description = suggested_skill_description or ""
    suffix = f" — {description}" if description else ""
    return {
        "role": "system",
        "content": (
            f"Suggested skill for this task: {suggested_skill}{suffix}. "
            f"Invoke it via invoke_skill if appropriate for the work."
        ),
    }


def register_invoke_skill_tool(
    registry: ToolRegistry,
    *,
    skill_loader: SkillLoader | None,
    retriever: Any,
    completion_fn: Any,
) -> list[str]:
    """Register ``invoke_skill`` into ``registry`` — the WORKER-ONLY tool.

    Separate from ``knowledge_search`` (now built by the shared
    :func:`~backend.workflow.application.tool_registry.assemble_run_tool_registry`) because
    ``register_invoke_skill`` lives in ``backend.extensions.skill`` — a context the MCP transport
    is forbidden to import. So the in-process loop adds it here, after the shared factory; the MCP
    path leaves it out until a follow-up threads a ``SkillLoader`` + completion fn in from the
    composition root (INV-7 #1, follow-up).

    Only when a workspace :class:`SkillLoader` is provided (the production worker factory always
    threads one in). Returns the names added so the caller can fold them into the surfaced tool
    schema. A missing loader → empty list."""
    if skill_loader is None:
        return []
    searcher = _RetrieverSearcher(retriever) if retriever is not None else None
    # invoke_skill — runs a named workspace skill end-to-end. The skill runner's completion seam
    # routes through the SAME loop LLM (adapted to its (system_prompt, user_input) shape); the
    # optional searcher primes the skill's system prompt with retrieved knowledge.
    register_invoke_skill(
        registry,
        loader=skill_loader,
        completion_fn=completion_fn,
        searcher=searcher,
    )
    return [INVOKE_SKILL_NAME]


def _merge_conflict_directive(run: ExecutionRun) -> dict[str, Any] | None:
    """PR7 — the conflict-resolution instruction for a RE-DISPATCHED conflict.

    When the ``github_merge_watch`` worker's authoritative freshness merge finds
    the run's PR branch genuinely conflicts with the base, it writes
    ``run.payload["merge_conflict"] = {conflict_paths, base_branch, pr_number}``
    and re-opens the run (RUNNING → OPEN). This turns that payload into a clear
    turn-context message telling the agent to resolve the conflict — and to
    raise the founder Decision (``ask_user_question``) ONLY when the merge is
    genuinely AMBIGUOUS, not for a mechanical resolution. ``None`` when the run
    carries no re-dispatched conflict (the loop is unchanged).

    The message describes the tree the agent will ACTUALLY find: the freshen
    already started the merge and left it stopped on the conflicts, in both
    execution models. It used to say "pull the latest base", which git refuses
    mid-merge — and the obvious escape from that refusal is ``merge --abort``,
    which destroys the one thing that makes the PR mergeable. Live run
    ``7442c185`` lost it by the neighbouring route: given a clean tree, the agent
    hand-edited the files into a LINEAR commit, reconciling the content while
    leaving base un-merged, so every later poll re-dispatched the same conflict."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    conflict = payload.get("merge_conflict")
    if not isinstance(conflict, dict):
        return None
    raw_paths = conflict.get("conflict_paths")
    paths = [str(p) for p in raw_paths] if isinstance(raw_paths, list) else []
    base = str(conflict.get("base_branch") or "the base branch")
    paths_str = ", ".join(paths) if paths else "(the conflicting files)"
    return {
        "role": "user",
        "content": (
            f"A merge of `{base}` into your branch is ALREADY IN PROGRESS in your "
            f"working tree and stopped on conflicts in: {paths_str}. Resolve them "
            "and COMMIT the merge. Do NOT abort it and do NOT hand-edit the files "
            "into a fresh commit instead: only a commit made from inside this "
            f"merge records `{base}` as an ancestor, and without that the pull "
            "request stays unmergeable no matter how correct the content is. "
            "If the correct resolution is MECHANICAL/clear (imports, "
            "adjacent non-overlapping edits, formatting), just resolve it and "
            "re-trigger verification. If it is AMBIGUOUS — two changes touched the "
            "SAME logic and picking the right merge needs a human judgment call — "
            "do NOT guess: call ask_user_question to raise the decision for the "
            "founder. Never paste raw conflict markers to the founder."
        ),
    }


def _consume_merge_conflict(run: ExecutionRun) -> None:
    """Clear the one-shot ``merge_conflict`` payload key after injecting it once.

    Removes the key so a later resume (RUNNING → OPEN → drive_loop again) does
    NOT re-inject the stale instruction, and leaves a persistent
    ``merge_conflict_resolving`` marker so the ask path (``mcp_work_effects.
    record_question``) classifies a question raised in this window as the
    founder-actionable ``merge_conflict_review`` Decision kind, not a vanilla
    ask. Re-assigns ``payload`` (not in-place mutate) so SQLAlchemy detects the
    change on the JSON column."""
    payload = dict(run.payload or {})
    if "merge_conflict" not in payload:
        return
    payload.pop("merge_conflict", None)
    payload["merge_conflict_resolving"] = True
    run.payload = payload


def _initial_user_message(run: ExecutionRun) -> dict[str, Any]:
    """#690 — the coding agent's first user turn: the WHOLE founder directive.

    Uses :func:`_intent_directive` (uncapped), NOT ``_intent_title`` (512-char
    label). Slicing here silently dropped requirements past 512 chars, so the
    agent built only the truncated half while verification passed on it."""
    return {"role": "user", "content": _intent_directive(run)}


async def is_client_attach_run(session: Any, run: ExecutionRun) -> bool:
    """#692 — True when this run executes natively on the user's own machine.

    Lazy import: the resolver lives under ``runtime/``, which imports this
    ``application/`` layer, so a module-level import would cycle. False for a run
    with no product, and never raises into the loop."""
    if run.product_id is None:
        return False
    from backend.workflow.application.runtime.account_resolution import (  # noqa: PLC0415
        product_is_client_attach,
    )

    try:
        return await product_is_client_attach(session, run.product_id)
    except Exception:  # noqa: BLE001 — an unreadable product must not break the loop
        logger.warning("client_attach_lookup_failed", run_id=str(run.id), exc_info=True)
        return False


def client_attach_terminal(
    run: ExecutionRun,
    work_step: Any,
    attempt: Any,
    *,
    gate: dict[str, Any] | None = None,
    final_text: str = "",
) -> LoopResult:
    """#692 — the terminal for a client_attach run: done, pending founder review.

    The work happened on the user's machine through the CLI's native tools, so
    the server holds no copy of the source. The step completes and returns the
    ``verified`` outcome (→ ``review_ready``); the founder reviews the changes in
    their own workspace.

    ``gate`` (in-place verify) is the blob from
    :func:`~backend.workflow.application.inplace_gate.run_inplace_gate`. Only a
    gate that actually RAN a command on that machine and passed it lifts
    ``proof_state`` to ``PROVED`` — that proof is the command's exit code, the
    same evidence a server-sandbox verify rests on. Every other case (no gate
    could be derived, nothing runnable was found, the tools were missing) leaves
    UNTESTED: the server proved nothing, and claiming otherwise would be false."""
    from backend.workflow.application.agent_loop import LoopResult as _LoopResult  # noqa: PLC0415
    from backend.workflow.infrastructure.db import (  # noqa: PLC0415
        ProofState,
        RunAttemptPhase,
        WorkStepStatus,
    )

    if gate is not None and gate.get("proved"):
        work_step.proof_state = ProofState.PROVED
    work_step.status = WorkStepStatus.VERIFIED
    attempt.phase = RunAttemptPhase.COMPLETED
    attempt.finished_at = datetime.now(UTC)
    return _LoopResult(
        outcome="verified",
        run_id=run.id,
        work_step_id=work_step.id,
        run_attempt_id=attempt.id,
        # The agent's own closing words. Discarded as ``""`` until now, which
        # made every client_attach run report nothing back to its caller — and
        # left the fallback deliverable body empty for a run with no file list.
        summary=final_text,
    )


__all__ = [
    "_ASK_SYSTEM_PROMPT",
    "system_prompt_for",
    "_RetrieverSearcher",
    "_SYSTEM_PROMPT",
    "_initial_user_message",
    "_intent_directive",
    "_intent_title",
    "_resumption_messages",
    "prior_step_message",
    "knowledge_seed_message",
    "make_knowledge_search_handler",
    "register_invoke_skill_tool",
    "_consume_merge_conflict",
    "_merge_conflict_directive",
    "client_attach_terminal",
    "is_client_attach_run",
    "suggested_skill_message",
]


async def settle_client_attach(
    orch: Any,
    *,
    run: ExecutionRun,
    work_step: Any,
    attempt: Any,
    box: Any,
    messages: list[dict[str, Any]],
    baseline: str | None,
    cycle: int,
    final_text: str = "",
) -> LoopResult | None:
    """#692 — settle a client_attach run once the model believes it is done.

    Returns the terminal :class:`LoopResult`, or ``None`` to tell the cycle to
    keep going (an honest gate failure the agent can still fix).

    The agent acted with the CLI's OWN tools on the user's machine: no
    server-visible ``written_paths`` to nudge about and no server-side source to
    merge. (Without this the loop nudges "you have not changed any file" and
    never settles — live E2E 2026-08-05: 3 hours re-acting on the user's clone.)

    In-place verify: the repo's OWN derived gate can still run — on that machine,
    where its source and toolchain are. The exit code is the verdict exactly as
    in the sandbox, so a pass is a real proof. A ``None`` gate means the repo
    declares no toolchain: legitimately gateless.
    """
    from backend.workflow.application.audit_events import LoopTerminal  # noqa: PLC0415
    from backend.workflow.application.client_attach_delivery import (  # noqa: PLC0415
        commit_and_push_run_work,
        land_client_attach_deliverable,
    )
    from backend.workflow.application.inplace_gate import (  # noqa: PLC0415
        changed_paths,
        gate_failure_is_actionable,
        run_inplace_gate,
    )
    from backend.workflow.application.verify_environment import (  # noqa: PLC0415
        open_run_check_environment,
    )

    gate = None
    if getattr(box, "runs_in_place", False):
        # The disposable environment is opened HERE and not inside the gate: it
        # is scoped to the verification, and holding it around the gate call is
        # what guarantees it is torn down whichever way that call returns.
        async with open_run_check_environment(
            session=orch._session, run=run, box=box
        ) as environment:
            gate = await run_inplace_gate(
                orch._verifier(),
                run=run,
                box=box,
                baseline=baseline,
                environment=environment,
            )
    if gate is not None and gate_failure_is_actionable(gate) and cycle + 1 < orch._max_cycles:
        # A real failure on the founder's machine — feed it back and let the
        # agent fix it, exactly as a failed sandbox verdict does. Only a command
        # that RAN and failed qualifies: a deriver fault or an unreachable
        # machine is not the agent's to repair, and asking it to try burns every
        # remaining cycle.
        # Same contract as the sandbox verdict: the failing command leads. A
        # prefix of the command list spends the budget in declaration order, so
        # a gate whose first checks pass can report its failure to nobody.
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your repository's own verification gate FAILED on this machine:\n"
                    f"{render_failed_commands(gate['commands'])}\n"
                    "Fix the problem and try again, then send your summary."
                ),
            }
        )
        return None
    # The run COMMITS its own work before settling — after the gate, so the
    # agent's fixes for a gate failure are part of what lands. #723 turned this
    # off for client_attach ("the server has no checkout to commit from"), and
    # the consequence was every such run leaving its work uncommitted in the
    # founder's tree: unattributable, mixed between runs, and read by a later
    # session as "that run produced nothing" when it had produced everything.
    # The checkout is on their machine, this box reaches it, and since #734 the
    # run has a worktree of its own there.
    #
    # Asked BEFORE the commit, while both git answers are still available: after
    # it, ``git status`` is clean by construction and only the baseline diff can
    # speak — and a tree with no baseline (not a git repo) would then report
    # nothing changed for a run that changed everything.
    changed = await changed_paths(box, baseline)
    delivery = await commit_and_push_run_work(box=box, run=run, baseline=baseline)

    result = client_attach_terminal(run, work_step, attempt, gate=gate, final_text=final_text)
    # The founder's half: a Deliverable to approve, a telegram, a PR (#738).
    # After the terminal transitions so a landing failure cannot leave the run
    # neither settled nor visible, and BEFORE the flush that persists them.
    await land_client_attach_deliverable(
        orch._session,
        run=run,
        attempt_id=attempt.id,
        changed_paths=changed,
        final_text=final_text,
        gate=gate,
        redis_client=orch._redis_client,
        settings=orch._settings,
    )
    await orch._session.flush()
    await orch._audit(
        run,
        attempt,
        LoopTerminal,
        {
            "outcome": "verified",
            "mode": "client_attach",
            "gate_proved": bool(gate and gate.get("proved")),
            "git": delivery.as_record(),
        },
    )
    return result
