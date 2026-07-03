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

from backend.workflow.domain.gate_derivation import (
    DerivedGate,
    derivation_planner_messages,
    parse_derived_gate,
)
from backend.workflow.domain.gate_discovery import discover_gate
from backend.workflow.domain.gate_scaffold import detect_stack
from backend.workflow.domain.honesty import compute_honesty_grade
from backend.workflow.domain.outcome_demonstration import (
    DemonstrationOutcome,
    DemonstrationPlan,
    Observation,
    ProbeResult,
    judge_probe,
    parse_demonstration_plan,
    summarize,
)
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
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxSession

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

#: I1 — the TARGET's OWN gate. "verified" must mean the deliverable passes the
#: project's own definition of done, discovered from the repo (its real CI /
#: Makefile / package.json / Cargo / go.mod), not a hardcoded Python check list.
#: The narrow changed-files L1 lets a change pass verify yet fail the repo's real
#: whole-repo CI (findings 2026-07-01, #413). Here we run the repo's own STATIC
#: gate (lint / format / type / import-contracts) whole-repo, ALWAYS, fail-closed.
#:
#: The verify sandbox is NOT the CI environment (no DB / node deps / secrets), so
#: the repo's setup + service-dependent + dynamic-test steps are NOT runnable
#: here and would false-fail a good change. We skip those — they run in the real
#: CI at PR time and their behaviour is covered by the L2 acceptance check — and
#: run only the source-deterministic STATIC checks that isolate cleanly.
GATE_CMD_TIMEOUT_S = 300.0
#: Command substrings that mark a discovered gate step as NOT a runnable static
#: check in the isolated sandbox: environment setup, dependency install, live
#: services, and dynamic test runners (deferred to L2 / real CI).
_GATE_SKIP_SUBSTRINGS = (
    # setup / dependency install (needed to RUN checks, not a check itself)
    "uv sync",
    "uv python install",
    "uv venv",
    "pip install",
    "poetry install",
    "bundle install",
    "cargo fetch",
    "go mod download",
    # node ecosystem — needs an install we skip, and the CI usually runs these
    # in a sub-package dir via ``working-directory`` (lost by discovery), so a
    # bare ``pnpm lint`` from the repo root fails on a missing manifest. Not a
    # runnable isolated static check → deferred to real CI at PR time.
    "corepack",
    "pnpm ",
    "npm ",
    "npx ",
    "yarn ",
    # dynamic test runners (behaviour → L2 acceptance / L-I2 / real CI). NOTE:
    # match test RUNNERS specifically — never a bare " test" substring, which
    # also matches a static check's args (e.g. ``ruff check backend/ tests/``,
    # the exact #413 check) and would silently disable it.
    "pytest",
    "unittest",
    "vitest",
    "jest",
    "tox",
    "nox",
    "go test",
    "cargo test",
    "gradle test",
    "mvn test",
    "dotnet test",
    "make test",
    "just test",
    # live services / db / orchestration
    "alembic ",
    "docker",
    "compose",
    # a mis-parsed 'uses:' fragment, never a shell command
    "actions/",
)

#: Bytes of each changed source file fed to the demonstration planner (enough
#: for a utility/module; the planner needs the API, not the whole repo).
_SOURCE_CTX_BYTES = 8 * 1024
#: Bytes of each manifest fed to the gate deriver — a manifest declares the
#: toolchain (deps, extras, scripts, targets), which fits in a small window.
_MANIFEST_CTX_BYTES = 8 * 1024
#: Repo-root declaration files fed to the gate deriver as GROUNDING. These are
#: universal manifest NAMES (not per-stack command logic — that is exactly the
#: coupling the deriver removes): whatever exists is shown to the LLM, which
#: reasons about the stack. Absent files are silently skipped.
_MANIFEST_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "justfile",
    "pom.xml",
    "build.gradle",
)
#: Extensions the demonstration planner can meaningfully exercise as CODE. A
#: prose/data deliverable (``.md`` / ``.txt`` / ``.json``) yields no sources →
#: no plan → ``undemonstrable`` (honest downgrade, not a fail). This is the
#: sandbox-shaped starting set (python-centric env); broadening to more stacks
#: is a later strategy-dispatch lift. NOTE this is only a gate on WHICH files
#: inform the planner — the plan's probes are stack-agnostic (the verifier picks
#: how to exercise the code).
_CODE_EXTS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".cs",
        ".cpp",
        ".cc",
        ".c",
        ".h",
        ".php",
        ".sh",
        ".sql",
    }
)


#: Files that legitimately change without being part of the task's intent — the
#: deterministic scope pre-filter drops them before the intent judge sees them,
#: so a routine lockfile bump is never flagged as a spurious change.
_SCOPE_EXEMPT_BASENAMES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "go.sum",
    }
)


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return "/tests/" in f"/{path}" or base.startswith("test_") or base.endswith("_test.py")


def _is_scope_exempt(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in _SCOPE_EXEMPT_BASENAMES


def _scope_judge_messages(intent: str, paths: list[str]) -> list[dict[str, str]]:
    listing = "\n".join(f"- {p}" for p in paths)
    # A dedicated, ISOLATED judge — it sees ONLY the intent + the changed paths,
    # never the retriever's canonical criteria (#473: retrieved knowledge must
    # never pollute a gating/flagging judgement). It flags relatedness, not
    # correctness, so paths + intent are enough (no file contents needed).
    system = (
        "You are a scope reviewer. Given a TASK and the list of files a change modified, "
        "identify files that are UNRELATED to the task — spurious or out-of-scope edits a "
        "focused change would not touch.\n"
        "A file is IN scope if it plausibly implements, tests, documents, or configures the "
        "task. Be CONSERVATIVE: only flag a file you are confident is unrelated to the stated "
        "task. When in doubt, do not flag it.\n"
        'Output ONLY a JSON object: {"flagged": ["path", ...], "reasoning": "<short>"}. '
        "An empty list means every change is in scope."
    )
    user = f"TASK:\n{intent}\n\nCHANGED FILES:\n{listing}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _is_code_path(path: str) -> bool:
    dot = path.rfind(".")
    return dot != -1 and path[dot:].lower() in _CODE_EXTS


def _path_to_module(path: str) -> str:
    """``backend/common/mean.py`` → ``backend.common.mean`` (import hint)."""
    return path.removesuffix(".py").replace("/", ".")


def _extract_json_object(text: str) -> Any:
    """Pull the first JSON object out of an LLM reply (tolerant of prose /
    ``` fences around it). Returns ``None`` when nothing parses."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None


def _demonstration_planner_messages(
    intent: str, sources: list[tuple[str, str]]
) -> list[dict[str, str]]:
    blob = "\n\n".join(f"# {p}\n{src}" for p, src in sources)[:12000]
    py_imports = [p for p, _ in sources if p.endswith(".py")]
    hints = (
        "\nFor a Python module, import it by its dotted path, e.g.:\n"
        + "\n".join(
            f'  python -c "from {_path_to_module(p)} import ...; print(...)"' for p in py_imports
        )
        if py_imports
        else ""
    )
    # The verdict is decided by a DETERMINISTIC comparison of what the probe
    # prints/exits vs what this plan declares — no model re-judges it. So the
    # planner is pushed to (a) exercise the FINISHED deliverable itself, not
    # re-run the author's tests, and (b) declare a LITERAL observation it is
    # confident about. An undemonstrable deliverable → empty probes (honest).
    system = (
        "You are an INDEPENDENT outcome-demonstration verifier. You do NOT re-run the "
        "author's tests and you do NOT give an opinion. You design executable PROBES that "
        "EXERCISE the finished deliverable and OBSERVE whether the TASK's intended RESULT "
        "actually happens.\n"
        "For each probe output:\n"
        '  - "name": what outcome it demonstrates\n'
        '  - "command": a shell command that exercises the deliverable and prints an '
        "observable result (call the function, run the built artifact, hit the endpoint, "
        "grep the produced file). Prefer a single self-contained command. Keep it ONE line: "
        "for Python use `python -c` with statements joined by ';' (NOT literal \\n — inside "
        "`python -c` a backslash-n is not a newline and raises SyntaxError). For anything "
        "multi-line, write a here-doc (`python - <<'PY' … PY`) instead.\n"
        '  - "expect_stdout_contains": the exact substring(s) that MUST appear in the '
        "output if the intent is satisfied\n"
        '  - "expect_exit_zero": true if the command must succeed, false if it must fail '
        "(e.g. the deliverable must REJECT bad input)\n"
        "RULES:\n"
        "- Derive expectations from the TASK, exercise the ACTUAL produced code.\n"
        "- Assert ONLY observations you are confident are correct — a wrong assertion "
        "wrongly fails good work.\n"
        '- Optional "setup" (list of prep commands, e.g. a build) runs first and is NOT '
        "asserted.\n"
        "- If the deliverable CANNOT be exercised by an executable probe (pure prose / "
        "design / half-built), return an empty probes list — that is a valid, honest answer.\n"
        '- Output ONLY a JSON object: {"setup": [...], "probes": [ {...} ]}. No prose.' + hints
    )
    user = f"TASK (the intended result):\n{intent}\n\nPRODUCED deliverable:\n{blob}"
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
        all_cmd_pass = all(r["passed"] for r in command_results)

        # I2 — outcome demonstration (the "half judge"). A SEPARATE verifier
        # plans executable probes that EXERCISE the finished deliverable and
        # declares the literal observation each must produce; the harness runs
        # them and the verdict is a DETERMINISTIC observation==expectation
        # comparison (no model in the verdict loop). A probe that RAN and
        # contradicted its expectation fails verification — this is what stops
        # garbage passing "verified" (Q-2). A deliverable that can't be exercised
        # (no probes / all unavailable) is NOT failed — best-effort downgrade
        # (founder decision #1); it just doesn't earn the strong grade.
        demonstration = await self._run_outcome_demonstration(run, written_paths, box)
        demo_pass = demonstration is None or demonstration["verdict"] != "failed"

        # I3 — scope discipline. For a product run with a real worktree (a
        # durable repo diff that will be delivered/reviewed), flag changed files
        # that look UNRELATED to the task intent — the spurious-change failure
        # mode (findings 2026-07-01: a "add one README line" task produced 12
        # spurious files and still passed verify). This is a SURFACE, not a gate:
        # founder decision #3 is "no-implicit → surface" (flag it, never silently
        # fix or block), so it does NOT enter the pass computation below — the
        # honesty grade + proof surface carry it to the founder (L-I3b).
        scope = await self._run_scope_check(run, written_paths)

        # I1 — the repo's OWN static gate (whole-repo lint/format/type/contracts),
        # run in the sandbox regardless of what the agent declared. A genuine gate
        # failure fails verification (fail-closed): this is what makes "verified"
        # mean "passes the target's own definition of done" rather than a narrow
        # changed-files subset (#413). ``None`` (no worktree / no runnable gate)
        # leaves the decision to the command + judge checks.
        project_gate = await self._run_project_gate(run, box)
        gate_pass = project_gate is None or bool(project_gate["passed"])

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

        # I1′ — the repo's OWN gate, DERIVED by an LLM grounded in the repo's
        # manifests (the general replacement for the hardcoded quality bar +
        # per-stack detectors). When present it is the AUTHORITATIVE command
        # gate, and the agent's declared command_results become ADVISORY —
        # recorded for the proof surface but never gating, so an invented
        # `--extra dev` / `python -m ruff` that fails on the sandbox can no
        # longer false-fail the run (the F7 retry loop). A deriver hiccup /
        # non-product / non-applicable repo → None → fall back to the agent +
        # mandatory command attestation, so nothing regresses while the old
        # path still stands (removed in a later increment).
        derived_gate = await self._run_derived_gate(run, box, written_paths)
        command_gate_pass = (
            bool(derived_gate["passed"]) if derived_gate is not None else all_cmd_pass
        )

        passed = command_gate_pass and judge_pass and gate_pass and demo_pass
        outcome = VerificationOutcome.PASSED if passed else VerificationOutcome.FAILED

        # The honesty ladder (redesign §4): grade a PASSING verdict by evidence
        # strength so "verified" is honest about HOW strongly it holds. Recorded
        # for the proof surface + the trust ratchet (D → founder review, L-I3c).
        # ``None`` for a non-product/Direct run — the repo-gate ladder is N/A.
        applicable = run.product_id is not None and self._is_real_worktree(run)
        gate_passed = bool(
            project_gate is not None
            and project_gate["passed"]
            and any(c["status"] == "passed" for c in project_gate["commands"])
        )
        demonstrated = demonstration is not None and demonstration["verdict"] == "demonstrated"
        honesty_grade = (
            compute_honesty_grade(
                applicable=applicable,
                gate_passed=gate_passed,
                gate_discovered=project_gate is not None,
                demonstrated=demonstrated,
            )
            if passed
            else None
        )
        # Was a gate reasonably EXPECTED here? A repo with a detectable stack is a
        # real project that should declare a definition of done; an early /
        # greenfield repo with no stack yet is legitimately gateless. The ratchet
        # uses this to tell a genuine grade-D weakness ("couldn't verify") from a
        # legitimate early-stage skip (founder distinction) — see needs_founder_review.
        gate_expected = applicable and self._stack_detected(run)

        vr = VerificationResult(
            id=uuid.uuid4(),
            run_id=run.id,
            work_step_id=work_step.id,
            workspace_id=run.workspace_id,
            outcome=outcome,
            contract=contract.to_dict(),
            result={
                "command_results": command_results,
                "derived_gate": derived_gate,
                "judge": judge_blob,
                "project_gate": project_gate,
                "outcome_demonstration": demonstration,
                "scope": scope,
                "honesty_grade": honesty_grade,
                "gate_expected": gate_expected,
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
                    "outcome": outcome.value,
                    "commands": len(command_results),
                    "project_gate": (
                        None
                        if project_gate is None
                        else {
                            "origin": project_gate["origin"],
                            "passed": project_gate["passed"],
                            "checks": len(project_gate["commands"]),
                        }
                    ),
                    "outcome_demonstration": (
                        None
                        if demonstration is None
                        else {
                            "verdict": demonstration["verdict"],
                            "probes": len(demonstration["probes"]),
                        }
                    ),
                    "scope": (
                        None
                        if scope is None
                        else {
                            "verdict": scope["verdict"],
                            "flagged": len(scope["flagged_paths"]),
                        }
                    ),
                    "honesty_grade": honesty_grade,
                    "gate_expected": gate_expected,
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
    def _stack_detected(run: ExecutionRun) -> bool:
        """True iff the run's worktree has a detectable stack manifest — a real
        project that should declare a gate. Used to tell a genuine grade-D
        weakness from a legitimate early-stage skip (offline, pure)."""
        from backend.storage.product_workspace import run_worktree_path  # noqa: PLC0415

        return detect_stack(run_worktree_path(run.id)) is not None

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

    async def _read_repo_manifests(self, box: SandboxSession) -> dict[str, str]:
        """Read the repo's OWN declaration files (whatever exists) to ground the
        gate deriver. Best-effort: a missing file raises :class:`SandboxError`
        on a real sandbox / returns empty on the fake — skipped either way."""
        manifests: dict[str, str] = {}
        for path in _MANIFEST_FILES:
            try:
                data = await box.read_file(path, _MANIFEST_CTX_BYTES)
            except SandboxError:
                continue
            text = data.decode("utf-8", errors="replace").strip()
            if text:
                manifests[path] = text
        return manifests

    async def _author_derived_gate(
        self, intent: str, manifests: dict[str, str], written_paths: list[str]
    ) -> DerivedGate | None:
        """Ask the independent deriver for this repo's verification commands,
        grounded in its manifests. Bounded — a hung executor CLI must never
        stall the run; a hiccup → ``None`` → the caller treats it as no gate
        (best-effort, never a false-fail)."""
        try:
            turn = await asyncio.wait_for(
                self._llm.complete(
                    messages=derivation_planner_messages(
                        manifests=manifests, changed_files=written_paths, intent=intent
                    ),
                    tools=None,
                ),
                timeout=_VERIFY_LLM_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 — a deriver hiccup must never break the run
            return None
        raw = _extract_json_object(str(getattr(turn, "content", "") or ""))
        if raw is None:
            return None
        return parse_derived_gate(raw)

    async def _run_derived_gate(
        self, run: ExecutionRun, box: SandboxSession, written_paths: list[str]
    ) -> dict[str, Any] | None:
        """I1′ — the repo's OWN verification gate, DERIVED by an LLM grounded in
        the repo's declarations (not a per-stack detector list nor a hardcoded
        ``uv run ruff`` bar). The derived commands RUN in the sandbox; the verdict
        is their exit codes, never a model's opinion. A command whose tool isn't
        here (exit 127) is ``unavailable`` — recorded, never a false-fail; a
        command that RAN and failed is a real gate failure.

        Returns ``None`` for a non-product / non-worktree run, or on a deriver
        hiccup (best-effort). Otherwise a blob: ``origin`` ("derived"),
        ``applicable`` (does a runnable gate apply — a code change vs pure prose),
        per-command ``commands`` (status ∈ passed|failed|unavailable), and
        ``passed`` (no command RAN and failed). An ``applicable`` repo whose
        commands could not be derived is applicable-but-empty (weak evidence)."""
        if run.product_id is None or not self._is_real_worktree(run):
            return None
        payload = run.payload or {}
        intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
        manifests = await self._read_repo_manifests(box)
        gate = await self._author_derived_gate(intent, manifests, written_paths)
        if gate is None:
            return None
        if not gate.applicable or gate.is_empty:
            return {
                "origin": "derived",
                "applicable": gate.applicable,
                "commands": [],
                "passed": True,
            }
        venv_ready = await self._ensure_project_venv(box)
        venv_bin = f"{box.workspace_mount}/.venv/bin"
        results: list[dict[str, Any]] = []
        for c in gate.commands:
            run_command = (
                f'export PATH="{venv_bin}:$PATH"; {c.command}' if venv_ready else c.command
            )
            res = await box.exec(run_command, timeout_s=GATE_CMD_TIMEOUT_S, shell=True)
            output = "\n".join(o for o in (res.stdout, res.stderr) if o)[-2000:]
            if res.exit_code == 0 and not res.timed_out:
                status = "passed"
            elif res.exit_code == 127:
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
                    "output": output,
                }
            )
        passed = not any(r["status"] == "failed" for r in results)
        return {
            "origin": "derived",
            "applicable": gate.applicable,
            "commands": results,
            "passed": passed,
        }

    async def _run_project_gate(
        self, run: ExecutionRun, box: SandboxSession
    ) -> dict[str, Any] | None:
        """I1 — run the repo's OWN static gate (lint/format/type/contracts).

        Returns ``None`` when the run has no real worktree or the repo declares
        no runnable static gate — the caller records that as weaker verification,
        not a pass. Otherwise a blob: ``origin`` (github-actions/makefile/...),
        per-command ``results`` (status ∈ passed|failed|unavailable), and
        ``passed`` (no command RAN and failed). A command that could not run here
        (exit 127) is ``unavailable`` — recorded, never a false-fail; a command
        that RAN and failed is a real gate failure → ``passed`` is False.
        """
        from backend.storage.product_workspace import run_worktree_path  # noqa: PLC0415

        if run.product_id is None or not self._is_real_worktree(run):
            return None
        gate = discover_gate(run_worktree_path(run.id))
        checks = [
            c for c in gate.commands if not any(s in c.command for s in _GATE_SKIP_SUBSTRINGS)
        ]
        if not checks:
            return None
        venv_ready = await self._ensure_project_venv(box)
        venv_bin = f"{box.workspace_mount}/.venv/bin"
        results: list[dict[str, Any]] = []
        for c in checks:
            run_command = (
                f'export PATH="{venv_bin}:$PATH"; {c.command}' if venv_ready else c.command
            )
            res = await box.exec(run_command, timeout_s=GATE_CMD_TIMEOUT_S, shell=True)
            output = "\n".join(o for o in (res.stdout, res.stderr) if o)[-2000:]
            if res.exit_code == 0 and not res.timed_out:
                status = "passed"
            elif res.exit_code == 127:
                status = "unavailable"
            else:
                status = "failed"
            results.append(
                {
                    "label": c.label,
                    "command": c.command,
                    "source": c.source,
                    "exit_code": res.exit_code,
                    "timed_out": res.timed_out,
                    "status": status,
                    "output": output,
                }
            )
        passed = not any(r["status"] == "failed" for r in results)
        return {"origin": gate.origin, "commands": results, "passed": passed}

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

    async def _run_outcome_demonstration(
        self,
        run: ExecutionRun,
        written_paths: list[str],
        box: SandboxSession,
    ) -> dict[str, Any] | None:
        """I2 — the "half judge". A SEPARATE verifier PLANS executable probes
        that exercise the finished deliverable and declares the literal
        observation each must produce; we run them and judge by a DETERMINISTIC
        ``observation == expectation`` comparison (:func:`judge_probe` — no model
        in the verdict loop). See :mod:`backend.workflow.domain.outcome_demonstration`.

        Returns a demonstration blob (``verdict`` ∈ demonstrated | failed |
        undemonstrable, plus per-probe observations) or ``None`` when there's
        nothing to demonstrate — no intent, or no exercisable CODE changed
        (prose/data → the deliverable is honestly undemonstrable here).

        ``verdict == "failed"`` means a probe RAN and the intended result was NOT
        observed → verification fails (Q-2: garbage no longer passes). Every
        other outcome (undemonstrable / all-unavailable / an LLM or sandbox
        hiccup → ``None``) is best-effort: it never false-fails, it just doesn't
        earn the strong grade (founder decision #1)."""
        payload = run.payload or {}
        intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
        if not intent:
            return None
        sources: list[tuple[str, str]] = []
        for path in written_paths:
            if not _is_code_path(path) or _is_test_path(path):
                continue
            try:
                data = await box.read_file(path, _SOURCE_CTX_BYTES)
            except SandboxError:
                continue
            sources.append((path, data.decode("utf-8", errors="replace")))
        if not sources:
            return None

        plan = await self._author_demonstration_plan(intent, sources)
        if plan is None or plan.is_empty:
            # Honest downgrade: the deliverable could not be reduced to an
            # executable demonstration. Not a fail — recorded so the grade /
            # proof surface (L-honesty) reflects the weaker evidence.
            outcome = DemonstrationOutcome(verdict="undemonstrable")
            blob = outcome.to_dict()
            blob["plan"] = plan.to_dict() if plan is not None else None
            return blob

        # Materialize the project venv so python probes resolve the deps, then
        # run the (unasserted) setup and each probe in the sandbox.
        venv_ready = await self._ensure_project_venv(box)
        venv_bin = f"{box.workspace_mount}/.venv/bin"

        def _wrap(command: str) -> str:
            return f'export PATH="{venv_bin}:$PATH"; {command}' if venv_ready else command

        for setup_cmd in plan.setup:
            # Best-effort prep (build / install). A failed setup is not asserted
            # — it just makes the dependent probes unavailable, which downgrades.
            await box.exec(_wrap(setup_cmd), timeout_s=GATE_CMD_TIMEOUT_S, shell=True)

        results: list[ProbeResult] = []
        for probe in plan.probes:
            res = await box.exec(_wrap(probe.command), timeout_s=VERIFY_TIMEOUT_S, shell=True)
            obs = Observation(
                exit_code=res.exit_code,
                stdout=res.stdout,
                stderr=res.stderr,
                timed_out=res.timed_out,
            )
            results.append(
                ProbeResult(probe=probe, observation=obs, status=judge_probe(probe, obs))
            )

        outcome = DemonstrationOutcome(verdict=summarize(results), results=tuple(results))
        logger.info(
            "outcome_demonstration",
            run_id=str(run.id),
            verdict=outcome.verdict,
            probes=len(results),
        )
        blob = outcome.to_dict()
        blob["plan"] = plan.to_dict()
        return blob

    async def _author_demonstration_plan(
        self, intent: str, sources: list[tuple[str, str]]
    ) -> DemonstrationPlan | None:
        """Ask the independent verifier for a demonstration plan (bounded — a
        hung executor CLI must never stall the run; a hiccup → ``None`` →
        undemonstrable, never a false-fail)."""
        try:
            turn = await asyncio.wait_for(
                self._llm.complete(
                    messages=_demonstration_planner_messages(intent, sources), tools=None
                ),
                timeout=_VERIFY_LLM_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 — planner hiccup must never break the run
            return None
        raw = _extract_json_object(str(getattr(turn, "content", "") or ""))
        if raw is None:
            return None
        return parse_demonstration_plan(raw)

    async def _run_scope_check(
        self, run: ExecutionRun, written_paths: list[str]
    ) -> dict[str, Any] | None:
        """I3 — flag changed files that look unrelated to the task intent.

        Gated to a product run with a real worktree — the durable repo diff that
        gets delivered / reviewed, where a spurious change actually matters (a
        Direct-path scratch run has no such diff). Returns a blob
        ``{verdict: clean|flagged, flagged_paths, reasoning}`` or ``None`` when
        not applicable (non-product / no worktree / no intent / no candidate
        files) or on an LLM hiccup. SURFACE only — the caller never folds this
        into the pass verdict (founder decision #3: flag, never block).

        The judge is ISOLATED: a fresh call seeing only the intent + the changed
        paths, so the retriever's canonical criteria can never pollute it (#473).
        The flagged set is intersected with the actual candidates, so the judge
        cannot invent a path."""
        if run.product_id is None or not self._is_real_worktree(run):
            return None
        payload = run.payload or {}
        intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
        if not intent:
            return None
        candidates = [p for p in written_paths if not _is_scope_exempt(p)]
        if not candidates:
            return None
        try:
            turn = await asyncio.wait_for(
                self._llm.complete(messages=_scope_judge_messages(intent, candidates), tools=None),
                timeout=_VERIFY_LLM_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 — a scope hiccup must never break the run
            return None
        raw = _extract_json_object(str(getattr(turn, "content", "") or ""))
        if not isinstance(raw, dict):
            return None
        candidate_set = set(candidates)
        raw_flagged = raw.get("flagged")
        flagged = [
            p
            for item in (raw_flagged if isinstance(raw_flagged, list) else [])
            if (p := str(item).strip()) in candidate_set
        ]
        logger.info(
            "scope_check",
            run_id=str(run.id),
            candidates=len(candidates),
            flagged=len(flagged),
        )
        return {
            "verdict": "flagged" if flagged else "clean",
            "flagged_paths": flagged,
            "reasoning": str(raw.get("reasoning") or "")[:500],
            "candidates": len(candidates),
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
