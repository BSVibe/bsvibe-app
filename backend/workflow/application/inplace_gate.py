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

import shlex
from typing import Any

import structlog

from backend.workflow.application.verification_service import (
    DerivedGateFailed,
    VerificationService,
)
from backend.workflow.domain.gate_derivation import surface_exercised
from backend.workflow.domain.verifier_contract import VerificationContract
from backend.workflow.infrastructure.db import VerificationOutcome, VerificationResult
from backend.workflow.infrastructure.sandbox import SandboxSession

logger = structlog.get_logger(__name__)

#: A git query on the founder's machine — a round trip, not a build.
_GIT_TIMEOUT_S = 30.0

#: exit 127 = command not found on that machine.
_MISSING_TOOL_EXIT = 127


async def capture_inplace_baseline(box: SandboxSession) -> str | None:
    """The founder tree's ``HEAD`` BEFORE the agent acts, or ``None``.

    Read once at the top of the run: afterwards the agent's own commits have
    moved ``HEAD``, so there is no way to recover where this run started. A tree
    that is not a git repo (or a machine that will not answer) yields ``None``,
    which is the honest reading — never a fabricated baseline.
    """
    try:
        res = await box.exec("git rev-parse HEAD", timeout_s=_GIT_TIMEOUT_S, shell=True)
    except Exception:  # noqa: BLE001 — an unreachable machine simply has no baseline
        return None
    if res.timed_out or res.exit_code != 0:
        return None
    head = res.stdout.strip().splitlines()
    return head[0].strip() if head and head[0].strip() else None


def _porcelain_paths(stdout: str) -> set[str]:
    """Paths out of ``git status --porcelain`` (renames at their NEW name)."""
    paths: set[str] = set()
    for line in stdout.splitlines():
        entry = line[3:].strip() if len(line) > 3 else ""  # noqa: PLR2004 — "XY " status prefix
        if not entry:
            continue
        # "R  old -> new": the new name is the one a gate can check.
        _, arrow, new = entry.partition(" -> ")
        paths.add((new if arrow else entry).strip().strip('"'))
    return paths


async def _workspace_probe_failure(box: SandboxSession) -> str | None:
    """``None`` when the workspace can be read; the reason when it cannot.

    A single cheap listing. It exists so the ladder's "no manifest → gateless"
    rung can only be reached from a workspace we demonstrably COULD have read a
    manifest from — otherwise an infrastructure fault (dead worker, wrong path)
    silently becomes a statement about the repo.
    """
    try:
        await box.list_dir(".")
    except Exception as exc:  # noqa: BLE001 — any failure to read = cannot conclude absence
        return f"{type(exc).__name__}: {exc}"
    return None


def gate_failure_is_actionable(gate: dict[str, Any]) -> bool:
    """Is this gate failure something the AGENT can fix by working again?

    Only a command that actually RAN and failed is. A deriver fault, an
    unreachable machine, or a tool missing from that machine (exit 127) are
    infrastructure facts: re-prompting the agent to "fix the problem" asks it to
    repair something outside its reach and burns the run's remaining cycles.
    """
    return any(c.get("status") == "failed" for c in gate.get("commands") or ())


async def changed_paths(box: SandboxSession, baseline: str | None) -> list[str]:
    """What THIS run changed in the founder's tree, as git sees it.

    Public because the settle path asks the same question for a different
    reason: the gate needs it to derive commands, and the deliverable landing
    needs it as the run's ``artifact_refs`` — and as the answer to "did this run
    change anything at all", which decides whether the founder gets an approval
    item.

    ``written_paths`` is always empty for a client_attach run — the agent used
    the CLI's native tools, so the server observed no writes. Passing that empty
    list to the deriver asserts "nothing changed", which is false: the truth is
    that the SERVER cannot see. The founder's tree can, so ask it there — commits
    made since ``baseline`` plus whatever is still uncommitted. Only names cross
    the wire, never contents, so the privacy contract holds.

    An unanswerable query contributes nothing rather than guessing. That keeps a
    run which genuinely changed nothing reporting nothing — a no-op must not
    collect a proof.
    """
    paths: set[str] = set()
    queries = ["git status --porcelain"]
    if baseline:
        queries.append(f"git diff --name-only {shlex.quote(baseline)}..HEAD")
    for query in queries:
        try:
            res = await box.exec(query, timeout_s=_GIT_TIMEOUT_S, shell=True)
        except Exception as exc:  # noqa: BLE001 — a failed probe adds nothing, never a guess
            # Logged, never silent: an unseen probe failure reads downstream as
            # "this run changed nothing", which is the same costume a wiring
            # break wore in the 2026-08-09 E2E.
            logger.info("inplace_changed_paths_probe_failed", query=query, error=str(exc))
            continue
        if res.timed_out or res.exit_code != 0:
            continue
        if query.startswith("git status"):
            paths |= _porcelain_paths(res.stdout)
        else:
            paths |= {line.strip() for line in res.stdout.splitlines() if line.strip()}
    return sorted(paths)


async def run_inplace_gate(
    service: VerificationService,
    *,
    run: Any,
    box: SandboxSession,
    baseline: str | None = None,
    environment: Any = None,
) -> dict[str, Any] | None:
    """Derive and run this repo's verification gate on the founder's machine.

    Returns the gate blob (``passed`` / ``proved`` / per-command records), or
    ``None`` when the repo declares no toolchain at all — the one case where
    "no gate ran" is not a failure but simply the truth about the repo.

    ``environment`` (a
    :class:`~backend.workflow.application.verify_environment.CheckEnvironment`)
    is WHERE the check commands run. Two boxes, deliberately:

    * the CHECKS go through ``environment.box`` — a disposable container, so a
      pass or a failure is attributable to the change and not to whatever that
      machine's ``.venv`` drifted into;
    * the manifest reads and git queries stay on ``box``, the founder's tree.
      They are about the source under test, and a container built from a
      declared toolchain has no reason to carry git — a silent ``127`` there
      would tell the deriver "nothing changed".

    ``None`` keeps the pre-#730 behaviour (everything on ``box``).

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

    # Make ABSENCE prove itself before believing it. ``_read_repo_manifests``
    # skips any file it cannot read, so an unreachable machine and a repo with no
    # build config both arrive here as zero manifests — and only one of them is
    # the honest "gateless" verdict below. In the 2026-08-09 E2E the gate box was
    # pointed at a path that did not exist on the founder's machine, and the
    # wiring break drained into that verdict wearing its legitimacy: the run
    # reported "this repo has no gate" and raised no alarm. One probe separates
    # them.
    # Nowhere honest to run the checks. Recorded fail-CLOSED and stopped here:
    # not the deriver (a gate nobody can run only burns a model call), and
    # certainly not a quiet fall back to the host, which would answer a
    # different question than the one this result will be read as answering.
    if environment is not None and environment.box is None:
        blob: dict[str, Any] = {
            "origin": "derived_in_place",
            "applicable": True,
            "commands": [],
            "passed": False,
            "proved": False,
            "environment": environment.describe(),
            "environment_unavailable": str(environment.unavailable),
        }
        await _persist(service, run=run, blob=blob, outcome=VerificationOutcome.FAILED)
        logger.warning(
            "inplace_gate_environment_unavailable",
            run_id=str(run.id),
            reason=str(environment.unavailable),
        )
        return blob

    #: Where the CHECKS run. Reads and git queries keep using ``box`` below.
    check_box: SandboxSession = environment.box if environment is not None else box
    described = environment.describe() if environment is not None else None

    probe = await _workspace_probe_failure(box)
    if probe is not None:
        blob = {
            "origin": "derived_in_place",
            "applicable": True,
            "commands": [],
            "passed": False,
            "proved": False,
            "workspace_probe_failed": probe,
            **({"environment": described} if described is not None else {}),
        }
        await _persist(service, run=run, blob=blob, outcome=VerificationOutcome.FAILED)
        logger.warning("inplace_gate_workspace_unreachable", run_id=str(run.id), error=probe)
        return blob

    manifests = await service._read_repo_manifests(box)
    if not manifests:
        logger.info("inplace_gate_no_manifest", run_id=str(run.id))
        return None

    payload = run.payload or {}
    intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
    changed = await changed_paths(box, baseline)

    # The agent's DECLARED contract runs here too. The two execution models must
    # differ only in WHERE commands run — not in what verification means. (This
    # was withheld while ``declare_verification`` sat on the workspace axis, on
    # the false premise that a declared contract needs a server-side worktree.
    # It does not: these commands run through the same box the derived gate uses.)
    declared = await _declared_checks(service, run, check_box)

    gate = await service._author_derived_gate(intent, manifests, changed)

    if isinstance(gate, DerivedGateFailed):
        # A manifest EXISTS, so a gate was expected here and we could not produce
        # one. Fail closed: not passed, and certainly not proved.
        blob = {
            "origin": "derived_in_place",
            "applicable": True,
            "commands": [],
            "passed": False,
            "proved": False,
            "gate_deriver_failed": gate.reason,
            **({"environment": described} if described is not None else {}),
        }
        await _persist(service, run=run, blob=blob, outcome=VerificationOutcome.FAILED)
        logger.info("inplace_gate_deriver_failed", run_id=str(run.id), reason=gate.reason)
        return blob

    if not gate.applicable or gate.is_empty:
        # The deriver ran and found nothing to run. Honest, but it proves nothing.
        # Nothing DERIVABLE to run — but a declared contract may still have run.
        passed = not any(r["status"] == "failed" for r in declared)
        blob = {
            "origin": "derived_in_place",
            "applicable": gate.applicable,
            "commands": declared,
            "passed": passed,
            "proved": passed and any(r["status"] == "passed" for r in declared),
            "surface_exercised": surface_exercised(declared),
            **({"environment": described} if described is not None else {}),
        }
        await _persist(
            service,
            run=run,
            blob=blob,
            outcome=VerificationOutcome.PASSED if passed else VerificationOutcome.FAILED,
        )
        return blob

    results = [*declared, *await _run_commands(service, gate=gate, box=check_box)]
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
        "surface_exercised": surface_exercised(results),
        **({"environment": described} if described is not None else {}),
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


async def _declared_checks(
    service: VerificationService, run: Any, box: SandboxSession
) -> list[dict[str, Any]]:
    """The agent's DECLARED contract, run in place — mapped to the gate's shape.

    Reuses the service's own command-check runner, so a declared check means the
    same thing in both execution models and there is no second implementation to
    drift. ``[]`` when nothing was declared (the derived gate is then the whole
    verification, exactly as before).
    """
    declared_raw = getattr(run, "declared_contract", None)
    if declared_raw is None:
        return []
    contract = await service.assemble_contract(
        declared_contract=declared_raw, written_paths=[], final_text=""
    )
    if contract is None:
        return []
    ran = await service._run_command_checks(contract, box)
    return [
        {
            "command": r.get("command", ""),
            "kind": "declared",
            # Same ladder as the derived commands: 127 means the tool is not on
            # that machine — recorded, never a false-fail.
            "status": (
                "passed"
                if r.get("passed")
                else ("unavailable" if r.get("exit_code") == _MISSING_TOOL_EXIT else "failed")
            ),
            "exit_code": r.get("exit_code"),
            "timed_out": r.get("timed_out", False),
            "output": r.get("output", ""),
        }
        for r in ran
    ]


async def _run_commands(
    service: VerificationService, *, gate: Any, box: SandboxSession
) -> list[dict[str, Any]]:
    """Run each derived command in the check environment; exit code is the verdict.

    PATH is prepended only when this box PROVISIONS a venv — i.e. a disposable
    container, which starts with nothing installed. Without it every derived
    command that is not ``uv run …`` would exit 127 and be recorded
    "unavailable": the isolation would have quietly cost us the very proof it
    was meant to make trustworthy. The founder's own box provisions nothing (a
    ``uv sync`` in their tree is an unasked-for mutation and their toolchain is
    already set up), so there the commands run bare exactly as before.
    """
    from backend.config import get_settings  # noqa: PLC0415

    timeout_s = get_settings().verify_gate_command_timeout_s
    venv_ready = await service._ensure_project_venv(box)
    venv_bin = f"{box.workspace_mount}/.venv/bin"
    results: list[dict[str, Any]] = []
    for c in gate.commands:
        # The RECORDED command stays the clean derived one — the PATH prefix is
        # an execution detail, not part of what was verified.
        command = f'export PATH="{venv_bin}:$PATH"; {c.command}' if venv_ready else c.command
        res = await box.exec(command, timeout_s=timeout_s, shell=True)
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


__all__ = [
    "capture_inplace_baseline",
    "changed_paths",
    "gate_failure_is_actionable",
    "run_inplace_gate",
]
