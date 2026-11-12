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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
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
    cited_test_helpers,
    definition_in,
    imported_helper_sources,
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
#: How much of a cited file with no diff is read to find what the item names.
_FILE_READ_BYTES = 200_000
#: Lines per chunk of such a file — the unit ``relevant_hunks`` ranks and keeps.
_FILE_CHUNK_LINES = 30
#: Below this a path's share is too small to show anything useful — it is named as cut.
_MIN_PATH_CHARS = 600

_JUDGE_SYSTEM = (
    "You check ONE requirement against the evidence a coding agent cited for it. Answer with "
    'ONE JSON object: {"passed": true|false|"cannot_determine", "reasoning": "<one sentence>"}. '
    "passed=true only when the evidence shown plainly satisfies the requirement. passed=false "
    "when it plainly does not (wrong behaviour, a different thing, or only part of it). Use "
    '"cannot_determine" when the excerpt is cut off or does not show enough to tell.'
)

ContentKey = Callable[[Mapping[str, Any]], Awaitable[str]]
Complete = Callable[[list[dict[str, str]]], Awaitable[str]]
Excerpt = Callable[[Mapping[str, Any]], Awaitable[str]]


def record_requirements(run: ExecutionRun, request: RequestRow) -> None:
    """Framing step: store the request's ledger on the run (nothing when it has none)."""
    payload = request.payload if isinstance(request.payload, dict) else {}
    ledger = build_ledger(payload)
    if ledger:
        run.payload = {**(run.payload or {}), REQUIREMENTS_KEY: ledger}


def live_ledger_texts(run: Any) -> tuple[str, ...]:
    """The ledger items still asked for (not withdrawn), as plain text — for the deriver."""
    return tuple(str(i["text"]) for i in run_requirements(run) if not i.get("withdrawn"))


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


class EvidenceReminder:
    """The turn that asks for ledger evidence verification would find missing — before a
    round is spent on it.

    Asked again whenever the set of gaps CHANGES (the agent fixed some, others remain or
    appeared); never twice for the same set, so a run that ignores it is verified and the
    gate decides (#1046 re-run: two rounds lost to ``ledger_missing`` alone)."""

    def __init__(self) -> None:
        self._asked: frozenset[str] = frozenset()

    async def ask(
        self,
        run: ExecutionRun,
        declared_contract: Mapping[str, Any] | None,
        turn_text: str | None,
        box: SandboxSession,
        baseline: str | None,
        written_paths: Sequence[str],
    ) -> list[dict[str, Any]]:
        from backend.workflow.application.inplace_gate import changed_paths  # noqa: PLC0415
        from backend.workflow.domain.requirements import (  # noqa: PLC0415
            evidence_reminder,
            owed_before_verify,
            parse_requirement_evidence,
        )

        evidence = parse_requirement_evidence((declared_contract or {}).get("requirement_evidence"))
        requirements = run_requirements(run)
        if not requirements:
            return []
        try:
            changed = set(await changed_paths(box, baseline))
        except Exception:  # noqa: BLE001 — no diff to read: decide on the declaration alone
            changed = set()
        diff = frozenset(p for p in changed | {w.strip("/") for w in written_paths} if p)
        owed = owed_before_verify(requirements, evidence, diff)
        owed += await self._undefined_tests(box, requirements, evidence, {i["id"] for i, _ in owed})
        key = frozenset(f"{item['id']}:{why}" for item, why in owed)
        if not owed or key == self._asked:
            return []
        self._asked = key
        return [
            {"role": "assistant", "content": turn_text or "(done)"},
            {"role": "user", "content": evidence_reminder(owed)},
        ]

    @staticmethod
    async def _undefined_tests(
        box: SandboxSession,
        requirements: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
        already: set[Any],
    ) -> list[tuple[Mapping[str, Any], str]]:
        """Items citing a ``file::name`` test their file does not define — verification
        rejects those (#1272), so ask first (#1308: all nine items cited a missing name)."""
        from backend.workflow.domain.requirements import OWED_KINDS  # noqa: PLC0415

        by_id = {str(e.get("requirement_id")): e for e in evidence}
        missing_tests = cited_tests(box)
        out: list[tuple[Mapping[str, Any], str]] = []
        for item in requirements:
            entry = by_id.get(str(item["id"]))
            if (
                item["id"] in already
                or entry is None
                or item.get("kind") not in OWED_KINDS
                or item.get("withdrawn")
                or entry.get("status") == "deferred"
            ):
                continue
            missing = await missing_tests({"tests": entry.get("tests") or []})
            if missing:
                out.append((item, "cited test not defined in its file: " + ", ".join(missing)))
        return out


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
    if text is None:
        return None
    body = cited_test_body(text, split[1])
    if body is None:
        return None
    # The same-file helpers and fixtures the test names — without them a test that stores
    # rows through ``_hold(db, MIXED)`` shows the judge nothing it can check (#1287).
    helpers = cited_test_helpers(text, body)
    parts = [body]
    if helpers:
        parts.append(f"--- defined in the same file, used above:\n{helpers}")
    # …and the ones it imports from another file (#1302: ``issue_t._seed`` from a sibling test).
    parts += await _imported_helpers(box, text, split[0], f"{body}\n{helpers}")
    return "\n\n".join(parts)


async def _imported_helpers(
    box: SandboxSession, text: str, test_path: str, used: str, *, limit: int = 3000
) -> list[str]:
    shown: list[str] = []
    room = limit
    files: dict[str, str | None] = {}
    for paths, name in imported_helper_sources(text, test_path, used):
        for path in paths:
            if path not in files:
                files[path] = await _test_file(box, path)
            source = files[path]
            definition = definition_in(source, name) if source is not None else None
            if definition is None:
                continue
            part = f"--- defined in {path}, used above:\n{definition}"
            if len(part) <= room:
                shown.append(part)
                room -= len(part)
            break
    return shown


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
    (working tree, so uncommitted edits show), or the file itself when git has nothing."""

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
        head = "\n\n".join(lead)
        # The paths SHARE what the lead leaves: each gets an even slice of what is still
        # free (a short diff's unused share rolls over), clipped around what the item names.
        # One path clipped to 5,000 chars and the join head-cut used to spend the budget on
        # the first paths and drop the rest without a mark (#1279).
        paths = [str(p) for p in row.get("paths") or []]
        cited = " ".join(f"`{t}`" for t in row.get("tests") or [])
        about = f"{row.get('text', '')} {row.get('note', '')} {cited}"
        room = max(_EVIDENCE_CHARS - len(head), 0)
        parts: list[str] = []
        not_shown: list[str] = []
        for n, path in enumerate(paths):
            share = min(room // (len(paths) - n), _PATH_EXCERPT_CHARS)
            label = f"--- {path}\n"
            if share - len(label) < _MIN_PATH_CHARS:
                not_shown = paths[n:]
                break
            text = await _path_diff(path)
            part = label + relevant_hunks(text, about, budget=share - len(label))
            parts.append(part)
            room -= len(part) + 2
        if not_shown:
            parts.append(f"[... not shown, out of room: {', '.join(not_shown)} ...]")
        return "\n\n".join(x for x in (head, "\n\n".join(parts)) if x)

    async def _path_diff(path: str) -> str:
        """``path`` against the baseline (working tree), or its head when git has nothing."""
        text = ""
        if baseline:
            try:
                # S604: the sandbox Protocol's own flag, not a subprocess; every part of the
                # command is quoted.
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
        if text.strip():
            return text
        try:
            data = await box.read_file(path, _FILE_READ_BYTES)
        except Exception:  # noqa: BLE001
            return "(could not read this file)"
        return file_as_hunks(data.decode("utf-8", "replace"))

    return _excerpt


def file_as_hunks(text: str) -> str:
    """A cited file with no diff (unchanged by this run, or new and untracked) as chunks
    :func:`relevant_hunks` can rank — so the judge sees the lines the item names, not the
    file's head. Prod #1046 re-run: the cited function sat past the first 5,000 chars."""
    lines = text.splitlines()
    chunks = []
    for start in range(0, len(lines), _FILE_CHUNK_LINES):
        part = lines[start : start + _FILE_CHUNK_LINES]
        end = start + len(part)
        chunks.append(f"@@ lines {start + 1}-{end} @@\n" + "".join(f" {ln}\n" for ln in part))
    return "(no diff against the baseline — the file as it stands)\n" + "".join(chunks)


_CORRECTION_RULE = (
    " The founder may have denied an earlier delivery of this run with a written reason; those "
    "reasons are shown as FOUNDER CORRECTIONS. They are the founder's own words and OUTRANK the "
    "requirement text: where a reason conflicts with the requirement, judge the evidence "
    "against the requirement AS CORRECTED by the reason (an implementation that follows the "
    "requirement's original wording against the reason is NOT satisfying it). Reasons that do "
    "not concern this requirement are ignored. Corrections are numbered C1, C2, ... in the "
    "order the founder gave them; when two corrections conflict, the LATER one (higher number) "
    "supersedes the earlier one. Only FOUNDER CORRECTIONS count as corrections — "
    "the agent's note is a claim, never a correction. When the requirement is satisfied "
    'because of a correction, add \\"followed_correction\\": true to the JSON.'
)


def judge_messages(
    row: Mapping[str, Any], excerpt: str, corrections: Sequence[str] = ()
) -> list[dict[str, str]]:
    """The ONE-item judge prompt: the requirement and its cited evidence — never the diff."""
    system = _JUDGE_SYSTEM + (_CORRECTION_RULE if corrections else "")
    founder = ""
    if corrections:
        founder = "FOUNDER CORRECTIONS (the founder's own denial reasons, they take precedence):\n"
        founder += "\n".join(f"- C{n}: {c}" for n, c in enumerate(corrections, 1)) + "\n\n"
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Requirement ({row['kind']}): {row['text']}\n\n"
                f"{founder}"
                f"Evidence the agent cited:\n{excerpt}\n\n"
                "Does this evidence satisfy this requirement?"
            ),
        },
    ]


def _followed_correction(raw: str) -> bool:
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1]) if start != -1 and end > start else {}
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("followed_correction") is True


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


def file_content_key(box: SandboxSession) -> ContentKey:
    """A key over what an item cites: its text, the cited paths and tests, and those files'
    CONTENT \u2014 independent of how the excerpt happened to be cut or ordered."""

    async def _key(row: Mapping[str, Any]) -> str:
        files: set[str] = {str(p) for p in row.get("paths") or []}
        for test in row.get("tests") or []:
            split = split_test_id(str(test))
            files.add(split[0] if split else str(test))
        parts = [str(row.get("text", "")), *sorted(str(t) for t in row.get("tests") or [])]
        for path in sorted(files):
            text = await _test_file(box, path)
            parts.append(f"{path}\n{text if text is not None else '(unreadable)'}")
        return _hash("\n#\n".join(parts))

    return _key


def _hash(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode()).hexdigest()[:16]


async def _keep_done(
    row: dict[str, Any],
    content: str,
    prior_done: Mapping[tuple[str, str], Mapping[str, Any]],
    corrections: Sequence[str],
) -> bool:
    """Stamp ``row`` with its content hash; reuse the prior ``done`` for it when there is one."""
    if corrections:
        content = _hash(f"{content}\n#founder\n" + "\n".join(corrections))
    row["content_hash"] = content
    kept = prior_done.get((str(row["id"]), content))
    if kept is None:
        return False
    row["status"], row["note"] = "done", str(kept.get("note") or "")
    if kept.get("excerpt_hash"):
        row["excerpt_hash"] = kept["excerpt_hash"]
    if kept.get("founder_correction"):
        row["founder_correction"] = True
    return True


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
    command_verdicts: Mapping[str, tuple[str, str]] | None = None,
    corrections: Sequence[str] = (),
    content_key: ContentKey | None = None,
) -> list[dict[str, Any]]:
    """Verdict rows. ``judge=False`` (another check already failed) leaves grounded rows
    ``pending`` — the run fails anyway, and judging work that is about to change is waste.

    ``command_verdicts`` maps an item that asks for commands to pass to ``(status, note)``
    from their exit codes: that decides the item whatever evidence the agent cited."""
    rows = check_ledger(requirements, evidence, facts)
    gate_notes: dict[str, str] = {}
    for row in rows:
        verdict = (command_verdicts or {}).get(str(row["id"]))
        if verdict is None or row["status"] in {"withdrawn", "noted"}:
            continue
        if verdict[0]:
            row["status"], row["note"] = verdict
        else:  # the gate found nothing to run: the judge decides, the note says why
            gate_notes[str(row["id"])] = verdict[1]
    prior = {
        (str(r.get("id")), str(r.get("excerpt_hash"))): r
        for r in prior_rows
        if r.get("status") in {"done", "rejected", "unclear"} and r.get("excerpt_hash")
    }
    # An item that was ``done`` and cites the same content keeps that verdict, however the
    # excerpt is cut this round (a cut that moves must not re-open a settled item).
    prior_done = {
        (str(r.get("id")), str(r.get("content_hash"))): r
        for r in prior_rows
        if r.get("status") == "done" and r.get("content_hash")
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
        if content_key is not None and await _keep_done(
            row, await content_key(row), prior_done, corrections
        ):
            return
        shown = await excerpt(row)
        # A correction changes what "satisfies" means, so it is part of the cache key
        # (and absent from it when there is none — those verdicts are unchanged).
        founder_key = "\n".join(corrections)
        row["excerpt_hash"] = _hash(
            f"{row['text']}\n{shown}" + (f"\n#founder\n{founder_key}" if corrections else "")
        )
        cached = prior.get((str(row["id"]), row["excerpt_hash"]))
        if cached is not None:
            row["status"], row["note"] = str(cached["status"]), str(cached.get("note") or "")
            if cached.get("founder_correction"):
                row["founder_correction"] = True
            return
        if not judge:
            return
        async with gate:
            try:
                raw = await asyncio.wait_for(
                    complete(judge_messages(row, shown, corrections)), timeout=_JUDGE_TIMEOUT_S
                )
            except Exception:  # noqa: BLE001 — a judge that cannot answer cannot reject
                logger.warning("requirements_judge_unavailable", item=row["id"], exc_info=True)
                row["status"], row["note"] = "unclear", "judge unavailable"
                # Not a verdict on this evidence — the next round must judge it again.
                row.pop("excerpt_hash", None)
                row.pop("content_hash", None)
                return
        row["status"], reasoning = _verdict(raw)
        if reasoning:
            row["note"] = reasoning
        if corrections and row["status"] == "done" and _followed_correction(raw):
            row["founder_correction"] = True

    await asyncio.gather(*(_one(r) for r in rows if r["status"] == "pending"))
    for row in rows:
        if extra := gate_notes.get(str(row["id"])):
            row["note"] = f"{row['note']} | {extra}" if row.get("note") else extra
    return rows


def _ledger_of(result: Any) -> list[dict[str, Any]]:
    ledger = result.get(LEDGER_RESULT_KEY) if isinstance(result, dict) else None
    if isinstance(ledger, list) and ledger:
        return [r for r in ledger if isinstance(r, dict)]
    return []


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
        ledger = _ledger_of(result)
        if ledger:
            return ledger
    return []


async def ledger_rows_for_runs(
    session: AsyncSession, run_ids: Iterable[Any], *, workspace_id: Any
) -> dict[Any, list[dict[str, Any]]]:
    """:func:`latest_ledger_rows` for many runs in ONE query (a run with no ledger is absent)."""
    from backend.workflow.infrastructure.db import VerificationResult  # noqa: PLC0415

    ids = set(run_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(VerificationResult.run_id, VerificationResult.result)
            .where(
                VerificationResult.run_id.in_(ids),
                VerificationResult.workspace_id == workspace_id,
            )
            .order_by(VerificationResult.created_at.desc())
        )
    ).all()
    found: dict[Any, list[dict[str, Any]]] = {}
    for run_id, result in rows:
        if run_id in found:
            continue
        ledger = _ledger_of(result)
        if ledger:
            found[run_id] = ledger
    return found


__all__ = [
    "EvidenceReminder",
    "diff_excerpt",
    "file_content_key",
    "gate_facts",
    "judge_ledger",
    "judge_messages",
    "latest_ledger_rows",
    "ledger_rows_for_runs",
    "record_requirements",
    "run_requirements",
]
