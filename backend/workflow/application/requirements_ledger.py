"""The requirements ledger at run time — recorded at framing, judged at the gate, read back.

``domain/requirements.py`` holds the pure parts. Here are the three places that need I/O:

* :func:`record_requirements` — framing. Stores the request's ledger on the run, once. Only
  deterministic extraction: a request with no structure has no ledger (v1).
* :func:`judge_ledger` — the gate. Grounds each item's cited evidence in what the gate can
  vouch for (:func:`gate_facts` — the run's diff as git sees it, and the commands that passed),
  then asks a judge ONE question per grounded item: "does this evidence satisfy this item?",
  showing the item and the diff of the cited paths only. A verdict is reused across rounds
  while the evidence it judged is unchanged, so a 10-round run does not re-judge every item
  10 times.
* :func:`latest_ledger_rows` — the PR body and the report read the verdict the gate reached
  from the newest verification row. The run's payload is NOT written during verification: an
  executor run's work-tool state lives there and is committed by another session.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from backend.workflow.domain.requirements import (
    LEDGER_RESULT_KEY,
    REQUIREMENTS_KEY,
    GateFacts,
    build_ledger,
    check_ledger,
    cited_test_body,
    relevant_hunks,
    split_test_id,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.workflow.infrastructure.db import ExecutionRun
    from backend.workflow.infrastructure.intake.db import RequestRow
    from backend.workflow.infrastructure.sandbox import SandboxSession

logger = structlog.get_logger(__name__)

#: One judge call per item, bounded so a hung model cannot stall verification.
_JUDGE_TIMEOUT_S = 120.0
#: How many item judges run at once.
_JUDGE_CONCURRENCY = 3
#: What the judge reads of the cited evidence, per path and in total.
_PATH_EXCERPT_CHARS = 5000
_EVIDENCE_CHARS = 10000

_JUDGE_SYSTEM = (
    "You check ONE requirement against the evidence a coding agent cited for it. Answer with "
    'ONE JSON object: {"passed": true|false|"cannot_determine", "reasoning": "<one sentence>"}. '
    "passed=true only when the evidence shown plainly satisfies the requirement. passed=false "
    "when it plainly does not (wrong behaviour, a different thing, or only part of it). Use "
    '"cannot_determine" when the excerpt is cut off or does not show enough to tell.'
)

Complete = Callable[[list[dict[str, str]]], Awaitable[str]]
Excerpt = Callable[[Mapping[str, Any]], Awaitable[str]]


def record_requirements(run: ExecutionRun, request: RequestRow) -> None:
    """Framing step: store the request's ledger on the run (nothing when it has none)."""
    payload = request.payload if isinstance(request.payload, dict) else {}
    ledger = build_ledger(payload)
    if ledger:
        run.payload = {**(run.payload or {}), REQUIREMENTS_KEY: ledger}


def run_requirements(run: ExecutionRun) -> list[dict[str, Any]]:
    """The ledger stored on the run (``[]`` for a run with none)."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    items = payload.get(REQUIREMENTS_KEY)
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("id") and i.get("text")]


def _passed_commands(
    command_results: Sequence[Mapping[str, Any]], derived_gate: Mapping[str, Any] | None
) -> tuple[str, ...]:
    out: list[str] = []
    for c in [*(derived_gate or {}).get("commands", []), *command_results]:
        if isinstance(c, Mapping) and (c.get("status") == "passed" or c.get("passed") is True):
            out.append(str(c.get("command") or ""))
    return tuple(c for c in out if c)


def evidence_owed_messages(
    run: ExecutionRun, declared_contract: Mapping[str, Any] | None, turn_text: str | None
) -> list[dict[str, Any]]:
    """The turn that asks for missing ledger evidence — ``[]`` when nothing is owed.

    Asked before verifying: a declaration that names none of an owed item is a failed round
    already, and the loop can see that without spending one."""
    from backend.workflow.domain.requirements import (  # noqa: PLC0415
        evidence_reminder,
        parse_requirement_evidence,
        unevidenced,
    )

    evidence = parse_requirement_evidence((declared_contract or {}).get("requirement_evidence"))
    owed = unevidenced(run_requirements(run), evidence)
    if not owed:
        return []
    return [
        {"role": "assistant", "content": turn_text or "(done)"},
        {"role": "user", "content": evidence_reminder(owed)},
    ]


async def gate_facts(
    *,
    box: SandboxSession,
    baseline: str | None,
    written_paths: Sequence[str],
    command_results: Sequence[Mapping[str, Any]],
    derived_gate: Mapping[str, Any] | None,
) -> GateFacts:
    """What the gate can vouch for. The diff is asked of git (so a ``shell_exec`` edit, a
    codegen or a ``git rm`` counts), unioned with the tool-observed writes."""
    from backend.workflow.application.inplace_gate import changed_paths  # noqa: PLC0415

    changed = set(await changed_paths(box, baseline)) | {p.strip("/") for p in written_paths}
    return GateFacts(
        diff_paths=frozenset(p for p in changed if p),
        passed_commands=_passed_commands(command_results, derived_gate),
    )


#: How much of a cited test file is read to find the test in it.
_TEST_FILE_BYTES = 512 * 1024


async def _test_file(box: SandboxSession, path: str) -> str | None:
    try:
        return (await box.read_file(path, _TEST_FILE_BYTES)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — an unreadable file says nothing either way
        return None


async def _cited_body(box: SandboxSession, test: str) -> str | None:
    split = split_test_id(test)
    if split is None:
        return None
    text = await _test_file(box, split[0])
    return cited_test_body(text, split[1]) if text is not None else None


MissingTests = Callable[[Mapping[str, Any]], Awaitable[list[str]]]


def cited_tests(box: SandboxSession) -> MissingTests:
    """The cited ``file::name`` tests that the file does not define.

    A name that is not there is a defect the agent can fix by citing the right one — not an
    "unclear" for the judge to guess at (#1269 cited a file's name, and a test that did not
    exist, and both passed as evidence). A file that cannot be read proves nothing either way
    and is not reported."""

    async def _missing(row: Mapping[str, Any]) -> list[str]:
        missing: list[str] = []
        for test in row.get("tests") or []:
            split = split_test_id(str(test))
            if split is None:
                continue
            text = await _test_file(box, split[0])
            if text is not None and cited_test_body(text, split[1]) is None:
                missing.append(str(test))
        return missing

    return _missing


def diff_excerpt(box: SandboxSession, baseline: str | None) -> Excerpt:
    """The judge's view of a row: the diff of each cited path against the run's baseline
    (working tree, so uncommitted edits show), or the file head when git has nothing."""

    async def _excerpt(row: Mapping[str, Any]) -> str:
        # The cited tests and the agent's note come FIRST and are never cut by the paths'
        # diffs: the shared budget used to be spent on two long diffs before a test body was
        # reached (#1275). The paths fill what is left.
        lead: list[str] = []
        for test in row.get("tests") or []:
            body = await _cited_body(box, test)
            lead.append(f"--- test {test}: passed at the gate" + (f"\n{body}" if body else ""))
        if row.get("note"):
            lead.append(f"--- agent's note: {row['note']}")
        parts: list[str] = []
        for path in row.get("paths") or []:
            text = ""
            if baseline:
                try:
                    # S604: the sandbox Protocol's own flag, not a subprocess; every part
                    # of the command is quoted.
                    result = await box.exec(  # noqa: S604
                        f"git -C {shlex.quote(box.workspace_mount)} diff {shlex.quote(baseline)}"
                        f" -- {shlex.quote(path)}",
                        timeout_s=10.0,
                        shell=True,
                    )
                    if result.exit_code == 0 and not result.timed_out:
                        text = result.stdout or ""
                except Exception:  # noqa: BLE001 — an unreadable diff falls back to the file
                    text = ""
            if not text.strip():
                try:
                    data = await box.read_file(path, _PATH_EXCERPT_CHARS)
                    text = (
                        f"(new or unreadable diff — file head)\n{data.decode('utf-8', 'replace')}"
                    )
                except Exception:  # noqa: BLE001
                    text = "(could not read this file)"
            # Cut around what the item names — the head of a long diff is rarely it.
            cited = " ".join(f"`{t}`" for t in row.get("tests") or [])
            about = f"{row.get('text', '')} {row.get('note', '')} {cited}"
            clipped = relevant_hunks(text, about, budget=_PATH_EXCERPT_CHARS)
            parts.append(f"--- {path}\n{clipped}")
        head = "\n\n".join(lead)
        room = max(_EVIDENCE_CHARS - len(head), 0)
        tail = "\n\n".join(parts)[:room]
        return "\n\n".join(x for x in (head, tail) if x)

    return _excerpt


def judge_messages(row: Mapping[str, Any], excerpt: str) -> list[dict[str, str]]:
    """The ONE-item judge prompt: the requirement and its cited evidence — never the diff."""
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Requirement ({row['kind']}): {row['text']}\n\n"
                f"Evidence the agent cited:\n{excerpt}\n\n"
                "Does this evidence satisfy this requirement?"
            ),
        },
    ]


def _verdict(raw: str) -> tuple[str, str]:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1]) if start != -1 and end > start else {}
    except ValueError:
        data = {}
    passed = data.get("passed") if isinstance(data, dict) else None
    reasoning = str(data.get("reasoning") or "") if isinstance(data, dict) else ""
    if passed is True or str(passed).lower() == "true":
        return "done", reasoning
    if passed is False or str(passed).lower() == "false":
        return "rejected", reasoning or "the evidence does not satisfy it"
    return "unclear", reasoning or "cannot determine"


def _hash(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode()).hexdigest()[:16]


async def judge_ledger(
    requirements: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    facts: GateFacts,
    *,
    excerpt: Excerpt,
    complete: Complete,
    prior_rows: Sequence[Mapping[str, Any]] = (),
    judge: bool = True,
    missing_tests: MissingTests | None = None,
) -> list[dict[str, Any]]:
    """Verdict rows. ``judge=False`` (another check already failed) leaves grounded rows
    ``pending`` — the run fails anyway, and judging work that is about to change is waste."""
    rows = check_ledger(requirements, evidence, facts)
    prior = {
        (str(r.get("id")), str(r.get("excerpt_hash"))): r
        for r in prior_rows
        if r.get("status") in {"done", "rejected", "unclear"} and r.get("excerpt_hash")
    }
    gate = asyncio.Semaphore(_JUDGE_CONCURRENCY)

    async def _one(row: dict[str, Any]) -> None:
        missing = await missing_tests(row) if missing_tests is not None else []
        if missing:
            row["status"] = "rejected"
            row["note"] = (
                f"cited test(s) not found in their file: {', '.join(missing)} — cite the test "
                "by the name it is defined with"
            )
            return
        shown = await excerpt(row)
        row["excerpt_hash"] = _hash(f"{row['text']}\n{shown}")
        cached = prior.get((str(row["id"]), row["excerpt_hash"]))
        if cached is not None:
            row["status"], row["note"] = str(cached["status"]), str(cached.get("note") or "")
            return
        if not judge:
            return
        async with gate:
            try:
                raw = await asyncio.wait_for(
                    complete(judge_messages(row, shown)), timeout=_JUDGE_TIMEOUT_S
                )
            except Exception:  # noqa: BLE001 — a judge that cannot answer cannot reject
                logger.warning("requirements_judge_unavailable", item=row["id"], exc_info=True)
                row["status"], row["note"] = "unclear", "judge unavailable"
                # Not a verdict on this evidence — the next round must judge it again.
                row.pop("excerpt_hash", None)
                return
        row["status"], reasoning = _verdict(raw)
        if reasoning:
            row["note"] = reasoning

    await asyncio.gather(*(_one(r) for r in rows if r["status"] == "pending"))
    return rows


async def latest_ledger_rows(session: AsyncSession, run_id: Any) -> list[dict[str, Any]]:
    """The ledger verdict of the run's newest verification that has one (``[]`` if none)."""
    from backend.workflow.infrastructure.db import VerificationResult  # noqa: PLC0415

    rows = (
        await session.execute(
            select(VerificationResult.result)
            .where(VerificationResult.run_id == run_id)
            .order_by(VerificationResult.created_at.desc())
            .limit(5)
        )
    ).scalars()
    for result in rows:
        ledger = (result or {}).get(LEDGER_RESULT_KEY) if isinstance(result, dict) else None
        if isinstance(ledger, list) and ledger:
            return [r for r in ledger if isinstance(r, dict)]
    return []


__all__ = [
    "diff_excerpt",
    "gate_facts",
    "judge_ledger",
    "judge_messages",
    "latest_ledger_rows",
    "record_requirements",
    "run_requirements",
]
