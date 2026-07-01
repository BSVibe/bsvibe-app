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

import asyncio
import json
import shlex
import uuid
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.domain.verifier_contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
)
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    ExecutionRunActivity,
    RunAttempt,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
)
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxResult, SandboxSession

logger = structlog.get_logger(__name__)

# Per-command verify timeout + the byte cap on each file fed to the judge.
# These are the canonical home (shared by both orchestrators); the native
# orchestrator re-imports them for back-compat.
VERIFY_TIMEOUT_S = 60.0
_JUDGE_FILE_CONTEXT_BYTES = 8 * 1024

# Hard ceiling on a verify-phase LLM call (the L2 acceptance author + the
# acceptance judge). ``self._llm`` can be an EXECUTOR account — a chat-shaped
# completion then dispatches an agentic CLI task whose own await runs for the
# full executor timeout (~1h). A hung/slow CLI there would block the whole run
# in ``review_ready``. These calls are bounded so a stuck executor degrades
# gracefully (author → skip the best-effort check; judge → an explicit
# non-pass) instead of hanging the run.
_VERIFY_LLM_TIMEOUT_S = 180.0

# Issue #361 — the sandbox image carries the toolchain (pytest/ruff/uv) but
# NOT the project's dependency tree, so a plain ``python -m pytest`` cannot
# import ``<project>.*`` (its conftest pulls the deps). For a uv-managed
# worktree we materialize the project venv (incl. extras, where pytest lives)
# once, then run each command check with that venv on PATH — so the natural
# command the agent declares resolves the full deps regardless of phrasing.
_UV_SYNC = "uv sync --frozen --all-extras"
# The cold sync downloads the dep tree (can be minutes) — its own generous
# budget, NOT the per-command VERIFY_TIMEOUT_S.
VENV_SYNC_TIMEOUT_S = 600.0

#: Rationale stamped on the judge check that folds retrieved BSage knowledge
#: (canon patterns / prior decisions / prior rejections) into the verify
#: contract. It is the stable marker the Delivery Report keys off to surface
#: those statements as a first-class ``references`` section ("근거 포함 답변" —
#: which past docs/decisions the agent referenced), distinct from the
#: verification checklist. Changing this string is a wire-contract change.
RETRIEVED_KNOWLEDGE_RATIONALE = "Canonical patterns retrieved for this change"

#: Pre-2026-06 deliverables stamped the retrieved-knowledge checks with the old
#: "BSage" (decommissioned product) wording. Kept ONLY so ``references_of`` still
#: extracts the references section from historical verifications; never emitted.
LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE = "BSage canonical patterns retrieved for this change"

#: Stamped on the L1 mandatory quality-gate checks (lint/format/type) that the
#: verifier appends regardless of the agent's declared contract.
MANDATORY_GATE_RATIONALE = "Mandatory project quality gate — enforced on the changed files"

#: Path prefixes mypy --strict covers in this repo (mirrors CI's ``mypy backend/``
#: + the sdk run). Changed files outside these get lint/format only.
_MYPY_PREFIXES = ("backend/", "plugin/", "bsvibe_sdk/")

#: Where the L2 independent acceptance test is written inside the sandbox.
_INDEPENDENT_TEST_REL = "tests/_bsvibe_independent_acceptance.py"
#: Bytes of each changed source file fed to the verifier-author (enough for a
#: utility/module; the author needs the API, not the whole repo).
_SOURCE_CTX_BYTES = 8 * 1024


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return "/tests/" in f"/{path}" or base.startswith("test_") or base.endswith("_test.py")


def _extract_python_code(text: str) -> str:
    """Pull the test source out of an LLM reply. Prefers a ```python fenced
    block; falls back to a bare ``` block; else the whole stripped reply."""
    for fence in ("```python", "```py", "```"):
        start = text.find(fence)
        if start == -1:
            continue
        body = text[start + len(fence) :]
        end = body.find("```")
        return (body[:end] if end != -1 else body).strip()
    return text.strip()


def _path_to_module(path: str) -> str:
    """``backend/common/mean.py`` → ``backend.common.mean`` (import hint)."""
    return path.removesuffix(".py").replace("/", ".")


def _acceptance_author_messages(
    intent: str, sources: list[tuple[str, str]], repair_hint: str = ""
) -> list[dict[str, str]]:
    blob = "\n\n".join(f"# {p}\n{src}" for p, src in sources)[:12000]
    imports = "\n".join(f"  from {_path_to_module(p)} import ...   # {p}" for p, _ in sources)
    # Engineered to yield a RUNNABLE test even from a weak model (the
    # minimum-spec target): concrete output contract, the exact import paths
    # spelled out, no prose. A broken authored test is discarded downstream — it
    # never false-fails good code — but a strong prompt keeps that rare.
    system = (
        "You are an INDEPENDENT acceptance-test author. Given a TASK and the implementation "
        "it produced, write ONE self-contained pytest test file that verifies the TASK's "
        "stated requirements against the implementation's public API.\n"
        "RULES:\n"
        "- Derive every assertion from the TASK REQUIREMENTS, not from any existing test.\n"
        "- Cover the happy path, the boundaries, and EACH error/edge case the task names "
        "(use pytest.raises for documented exceptions).\n"
        "- Import the modules under test by their exact dotted paths:\n" + imports + "\n"
        "- The file must be runnable as-is: real imports, valid Python, test_ functions.\n"
        "- Output ONLY the test file as a single ```python code block. No prose, no fences "
        "around anything else."
    )
    user = f"TASK (the requirements):\n{intent}\n\nIMPLEMENTATION under test:\n{blob}"
    if repair_hint:
        user += f"\n\n{repair_hint}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _mandatory_quality_checks(written_paths: list[str]) -> list[VerificationCheck]:
    """The deterministic quality bar the project enforces in CI (ruff check,
    ruff format --check, mypy --strict), scoped to the changed Python files.

    These run REGARDLESS of what the agent declared, so ``verified`` cannot mean
    "the one narrow command I chose passed" while the code is unformatted,
    lint-broken, or mistyped. Non-Python changes get nothing here (the gates
    would no-op / error). Returns ``[]`` when no ``.py`` file changed."""
    py = sorted({p for p in written_paths if p.endswith(".py")})
    if not py:
        return []
    files = " ".join(shlex.quote(p) for p in py)
    checks = [
        VerificationCheck(
            kind="command", command=f"uv run ruff check {files}", rationale=MANDATORY_GATE_RATIONALE
        ),
        VerificationCheck(
            kind="command",
            command=f"uv run ruff format --check {files}",
            rationale=MANDATORY_GATE_RATIONALE,
        ),
    ]
    typed = [p for p in py if p.startswith(_MYPY_PREFIXES)]
    if typed:
        checks.append(
            VerificationCheck(
                kind="command",
                command=f"uv run mypy {' '.join(shlex.quote(p) for p in typed)}",
                rationale=MANDATORY_GATE_RATIONALE,
            )
        )
    return checks


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

        # L1 — when the agent has staked a behavioral check (a declared
        # command), ALSO enforce the project's deterministic quality bar
        # (lint/format/type) on the changed files. This augments a real
        # attestation; it never manufactures one (no command → still None →
        # human review below), so a lint-clean but untested change isn't
        # silently called verified.
        if any(c.kind == "command" for c in checks):
            checks.extend(_mandatory_quality_checks(written_paths))

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
                        rationale=RETRIEVED_KNOWLEDGE_RATIONALE,
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
        passes), record a "verify" activity, and return the result.

        W2 — when ``run.product_id`` is set, prepend a merge step:

        1. ``commit_worktree`` — stage the agent's writes as a real
           commit on ``bsvibe/run/<rid>``
        2. ``merge_main_into_worktree`` — pull main in. A clean merge
           means the agent's branch is now a fast-forward of main;
           ``verify`` proceeds to command/judge checks. A conflict means
           the worktree carries conflict markers — verify fails with
           ``reason="merge_conflict"`` + conflict paths, and the agent's
           next loop round picks the markers up via standard
           file_read/file_edit tools (Claude Code-style auto-resolution).

        Non-product runs skip the merge step entirely — exactly the
        Direct-path test invariant.
        """
        # W2 — merge check first for product-bound runs. Gated on the
        # worktree actually being a real git worktree (the W1 provisioner
        # adds it; glue tests that bypass the provisioner have a plain
        # empty dir and should skip the merge step entirely).
        merge_conflict_paths: list[str] = []
        if run.product_id is not None and self._is_real_worktree(run):
            from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy
                commit_worktree,
                merge_main_into_worktree,
            )

            commit_message = f"work: {self._truncate_intent(run)} (run-{str(run.id)[:8]})"
            await commit_worktree(run.product_id, run.id, message=commit_message)
            merge_outcome = await merge_main_into_worktree(run.product_id, run.id)
            if merge_outcome.status == "conflict":
                merge_conflict_paths = merge_outcome.conflict_paths

        if merge_conflict_paths:
            # Skip command / judge checks — the worktree is in a
            # conflict state. Persist a FAILED VerificationResult so
            # the loop sees verification failed; the next agent round
            # will read the conflict markers from disk.
            vr = VerificationResult(
                id=uuid.uuid4(),
                run_id=run.id,
                work_step_id=work_step.id,
                workspace_id=run.workspace_id,
                outcome=VerificationOutcome.FAILED,
                contract=contract.to_dict(),
                result={
                    "merge_conflict": True,
                    "conflict_paths": merge_conflict_paths,
                },
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
                        "outcome": VerificationOutcome.FAILED.value,
                        "reason": "merge_conflict",
                        "conflict_paths": merge_conflict_paths,
                    },
                )
            )
            await self._session.flush()
            return vr

        command_results = await self._run_command_checks(contract, box)

        # L2 — independent acceptance check, ALWAYS run (a safety net is not
        # optional). A SEPARATE verifier authors a test from the INTENT (not the
        # worker's tests) and runs it; its result joins the command results, so a
        # genuine failure fails verification → the orchestrator routes to human
        # review — exactly when review is worth it (the independent check
        # disagreed with the worker). A broken/unusable authored test is
        # discarded inside the check (never a false-fail), so this is safe to run
        # unconditionally regardless of how weak the verify model is.
        independent = await self._run_independent_acceptance_check(run, written_paths, box)
        if independent is not None:
            command_results.append(independent)

        all_cmd_pass = all(r["passed"] for r in command_results)

        judge_blob: dict[str, Any] | None = None
        judge_pass = True
        # Split judge criteria: the AGENT'S OWN declared criteria vs the
        # retriever-added knowledge (rationale == RETRIEVED_KNOWLEDGE_RATIONALE).
        # The retriever fold is REFERENCE context ("근거 포함 답변"), pulled in by
        # loose semantic similarity — in a knowledge-rich workspace it drags in
        # statements unrelated to THIS task (dogfood dd2bd3a3: a rate-limiter got
        # "Toss Payments webhook HMAC" criteria).
        gating_criteria = [
            c
            for chk in contract.judge_checks
            if chk.rationale != RETRIEVED_KNOWLEDGE_RATIONALE
            for c in chk.criteria
        ]
        retrieved_criteria = [
            c
            for chk in contract.judge_checks
            if chk.rationale == RETRIEVED_KNOWLEDGE_RATIONALE
            for c in chk.criteria
        ]
        if gating_criteria:
            # The agent staked its OWN judge — grade ONLY that. The retriever's
            # criteria are NEVER merged into a real judge (they would false-fail
            # an otherwise-good agent judge; dogfood dd2bd3a3). They still surface
            # as Delivery-Report references via the persisted contract.
            judge_blob = await self._run_judge(gating_criteria, written_paths, final_text, box)
            judge_pass = bool(judge_blob.get("passed"))
        elif retrieved_criteria:
            # No agent judge — only the retriever fold. Lift E39/F6: when the
            # agent's command attestation already passed, the retriever judge is
            # ADVISORY (it reliably hallucinates against weak / unrelated criteria
            # + a truncated file view — skip it, don't flip a clean command-passed
            # run to FAILED). Otherwise it is the only verdict signal, so grade it.
            if command_results and all_cmd_pass:
                judge_blob = {"advisory": True, "skipped": "advisory_retrieval_only"}
            else:
                judge_blob = await self._run_judge(
                    retrieved_criteria, written_paths, final_text, box
                )
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

    @staticmethod
    def _is_real_worktree(run: ExecutionRun) -> bool:
        """True iff this run's workspace dir is a git worktree.

        A real worktree has a ``.git`` *file* (worktree pointer to the
        product's ``.git/worktrees/<name>``); a regular empty scratch
        dir from a non-product test has no ``.git`` entry at all. The
        W2 merge step is no-op'd in the latter case so glue tests that
        bypass the workspace provisioner aren't forced to also seed a
        git workspace.
        """
        from backend.storage.product_workspace import run_worktree_path  # noqa: PLC0415

        worktree = run_worktree_path(run.id)
        return (worktree / ".git").exists()

    @staticmethod
    def _truncate_intent(run: ExecutionRun, *, max_chars: int = 60) -> str:
        """Best-effort one-line summary of the run's intent for the
        agent commit subject. Reads from ``run.payload['intent_text']``
        when present; falls back to the run id when not. Newlines are
        stripped so the subject stays one line."""
        payload = run.payload or {}
        intent = payload.get("intent_text") if isinstance(payload, dict) else None
        text = intent if isinstance(intent, str) else f"run-{run.id}"
        text = text.replace("\n", " ").strip()
        return text[:max_chars]

    async def _run_command_checks(
        self, contract: VerificationContract, box: SandboxSession
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not contract.command_checks:
            return results
        # Issue #361 — ensure the project venv for a uv worktree, then run each
        # command inside it. ``venv_ready`` False (non-uv project, or a sync
        # failure) → commands run bare (honest fail, never a silent pass).
        venv_ready = await self._ensure_project_venv(box)
        venv_bin = f"{box.workspace_mount}/.venv/bin"
        for check in contract.command_checks:
            command = check.command or ""
            run_command = f'export PATH="{venv_bin}:$PATH"; {command}' if venv_ready else command
            res = await box.exec(run_command, timeout_s=VERIFY_TIMEOUT_S, shell=True)
            output = "\n".join(c for c in (res.stdout, res.stderr) if c)[-2000:]
            results.append(
                {
                    # The recorded command stays the agent's CLEAN declaration —
                    # the PATH prefix is an execution detail, not part of the
                    # contract surfaced in the Delivery Report.
                    "command": command,
                    "exit_code": res.exit_code,
                    "timed_out": res.timed_out,
                    "passed": res.exit_code == 0 and not res.timed_out,
                    "output": output,
                }
            )
        return results

    async def _ensure_project_venv(self, box: SandboxSession) -> bool:
        """For a uv-managed worktree, materialize ``.venv`` (incl. extras —
        pytest/ruff live there) so command checks resolve the project's full
        dependency tree regardless of how the agent phrased them (issue #361).

        Detection is by ``uv.lock`` presence (read, not exec — a missing lock
        raises :class:`SandboxError` on a real sandbox and returns empty on the
        host double, so a non-uv worktree never triggers a sync). Not a uv
        project → ``False`` so non-uv command checks (``npm test``, ``go
        test``, …) run unchanged. Best-effort: a sync failure also returns
        ``False`` — the command then runs bare and fails honestly rather than
        passing against a half-built environment."""
        try:
            lock = await box.read_file("uv.lock", 64)
        except SandboxError:
            return False
        if not lock:
            return False
        sync = await box.exec(_UV_SYNC, timeout_s=VENV_SYNC_TIMEOUT_S, shell=True)
        return sync.exit_code == 0 and not sync.timed_out

    async def _run_independent_acceptance_check(
        self,
        run: ExecutionRun,
        written_paths: list[str],
        box: SandboxSession,
    ) -> dict[str, Any] | None:
        """L2 — author a fresh acceptance test from the INTENT (via a separate
        LLM that does NOT see the worker's tests) and run it in the sandbox.

        Returns a command-result dict (folds into the PASS gate) or ``None`` when
        there's nothing to do — no intent, no changed source files, or the author
        produced no test. A best-effort failure (sandbox/LLM hiccup) returns
        ``None`` rather than failing the run: the worker's own contract + the L1
        gates still gate it; the independent check only ADDS confidence."""
        payload = run.payload or {}
        intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
        if not intent:
            return None
        sources: list[tuple[str, str]] = []
        for path in written_paths:
            if not path.endswith(".py") or _is_test_path(path):
                continue
            try:
                data = await box.read_file(path, _SOURCE_CTX_BYTES)
            except SandboxError:
                continue
            sources.append((path, data.decode("utf-8", errors="replace")))
        if not sources:
            return None
        # Re-validate the venv (a no-op-fast second `uv sync --frozen` if the
        # command checks already materialized it) so the authored test resolves
        # the project deps just like the agent's own tests.
        venv_ready = await self._ensure_project_venv(box)

        async def _author_and_run(repair_hint: str) -> SandboxResult | None:
            try:
                # Bounded — a hung executor CLI must never stall the run on this
                # best-effort check (TimeoutError is caught below → skip).
                turn = await asyncio.wait_for(
                    self._llm.complete(
                        messages=_acceptance_author_messages(intent, sources, repair_hint),
                        tools=None,
                    ),
                    timeout=_VERIFY_LLM_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 — author hiccup must never break the run
                return None
            code = _extract_python_code(str(getattr(turn, "content", "") or ""))
            if not code.strip():
                return None
            try:
                await box.write_file(_INDEPENDENT_TEST_REL, code.encode("utf-8"))
            except SandboxError:
                return None
            command = f"uv run pytest {_INDEPENDENT_TEST_REL} -q"
            venv_bin = f"{box.workspace_mount}/.venv/bin"
            cmd = f'export PATH="{venv_bin}:$PATH"; {command}' if venv_ready else command
            return await box.exec(cmd, timeout_s=VERIFY_TIMEOUT_S, shell=True)

        res = await _author_and_run("")
        if res is None:
            return None
        # Robust to a WEAK verify model (the minimum-spec target): a broken
        # authored test must NEVER false-fail good code. pytest exit codes
        # discriminate — 0=passed, 1=a test FAILED (a genuine disagreement →
        # gate → human review), 2/5=collection error / NO tests collected and
        # 3/4=internal/usage error (the AUTHOR produced an unusable test, not a
        # code defect). On a collection error retry ONCE, feeding the error back
        # for self-repair (mirrors the worker safety nets); if still unusable,
        # DISCARD — the worker contract + L1 gates still hold.
        if not res.timed_out and res.exit_code in (2, 5):
            err = "\n".join(c for c in (res.stdout, res.stderr) if c)[-1500:]
            retry = await _author_and_run(
                "Your previous test could not be COLLECTED/RUN (import or syntax error). "
                f"Fix it and output the corrected full test file. Error:\n{err}"
            )
            if retry is not None:
                res = retry
        if res.timed_out or res.exit_code not in (0, 1):
            logger.info(
                "independent_acceptance_unusable",
                run_id=str(run.id),
                exit_code=res.exit_code,
                timed_out=res.timed_out,
            )
            return None
        output = "\n".join(c for c in (res.stdout, res.stderr) if c)[-2000:]
        return {
            "command": "independent acceptance test (L2)",
            "exit_code": res.exit_code,
            "timed_out": False,
            "passed": res.exit_code == 0,
            "output": output,
            "independent": True,
        }

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
        try:
            # Bounded — if ``self._llm`` is an executor account, a hung agentic
            # CLI must not stall the run in verify. A timeout is an explicit
            # NON-pass (consistent with "never a silent pass") so the founder
            # reviews rather than the run rotting in ``review_ready``.
            turn = await asyncio.wait_for(
                self._llm.complete(messages=judge_messages, tools=None),
                timeout=_VERIFY_LLM_TIMEOUT_S,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return {"passed": False, "reasoning": "judge LLM timed out", "raw": ""}
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
