"""Compose the verified deliverable's SUMMARY — title, changed-file body, proof line.

Split out of :mod:`run_persistence` (which sat exactly at the repo's 600-line
ceiling) as a private sibling, the same shape ``_loop_context`` / ``_drive_loop``
take next to ``agent_loop``. One cohesive job lives here: turn a finished run
into the human-facing summary whose FIRST LINE becomes the PR title and the
settle note title — i.e. the sentence that becomes knowledge. Everything about
transitions, decisions and audit stays with the persistence conductor.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.workflow.infrastructure.db import ExecutionRun


# A coding-agent executor's ``--print`` output ends with a machine-readable
# ``<verification-contract>{…}</…>`` block (Lift E30). It's noise in a
# human-facing deliverable summary / PR body, so strip it.
_CONTRACT_BLOCK_RE = re.compile(
    r"<verification-contract>.*?</verification-contract>",
    re.DOTALL | re.IGNORECASE,
)
#: Cap the title line (first line of the summary → PR title / settle note
#: title) so a single-line intent doesn't produce a 512-char title.
_MAX_SUMMARY_TITLE = 120
#: Repairs streaming chunk-join whitespace artifacts ("done.Next" → "done. Next")
#: in the fallback prose — the coding-agent ``--print`` output concatenates
#: streamed chunks without the inter-sentence space.
_CHUNK_JOIN_RE = re.compile(r"([.!?:])([A-Z])")

#: Map a verification command to a friendly category for the summary line. We
#: surface the CATEGORY ("tests", "lint", …), never the raw command string —
#: echoing "uv run pytest …" would re-introduce the contract-block slop the F4
#: fix removed from the user-facing summary. Ordered = display order.
_CHECK_CATEGORY_LABELS: tuple[tuple[str, str], ...] = (
    ("pytest", "tests"),
    ("ruff check", "lint"),
    ("ruff format", "format"),
    ("mypy", "types"),
)


def _gate_command_results(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The in-place gate's commands, in ``command_results`` shape.

    A client_attach run is verified by the DERIVED gate (``derived_gate`` —
    per-command ``status``), the sandbox path by a declared contract
    (``command_results`` — per-command ``passed``). Same evidence, two record
    shapes, so map one onto the other rather than writing a second sentence
    builder: what the founder reads about a proof must not depend on WHERE the
    command ran. ``unavailable`` (exit 127) is not a pass — a tool missing from
    that machine proved nothing.
    """
    gate = result.get("derived_gate")
    if not isinstance(gate, Mapping):
        return []
    return [
        {
            "command": c.get("command"),
            "passed": c.get("status") == "passed",
            # Carried, not collapsed into ``passed``: "ran and passed" and "was
            # never able to run" are different claims and the founder is owed
            # both. The sandbox ``command_results`` shape has no third state, so
            # this key is simply absent there and reads as False.
            "unavailable": c.get("status") == "unavailable",
        }
        for c in gate.get("commands") or ()
        if isinstance(c, Mapping)
    ]


def _could_not_run_clause(commands: list[Any], ko: bool) -> str:
    """Name the gate checks that never ran, or "" when they all did.

    ``derived_gate["passed"]`` is ``not any(status == "failed")``, so a command
    whose tool is missing from that machine (exit 127 → ``unavailable``) does not
    fail the gate and the run reaches PROVED. The count sentence beside this one
    counts only PASSES, which meant a five-command gate with two unavailable read
    EXACTLY like a three-command gate that ran end to end — true, and incomplete
    in the one direction that matters. Measured 2026-09-02 on prod's own shape:
    "검증: 3개 확인 통과." and not one word about the two that were skipped, while
    that very run's retrospective note said "this sandbox has no docker binary".

    So say the number, name them, and say WHY — a missing tool is not a failure,
    and a reader who cannot tell the two apart will read a skipped check as a
    broken one. Returns a fragment APPENDED to the count sentence rather than a
    line of its own: ``_shipped_detail`` lifts a single line by its ``검증`` /
    ``Verified`` prefix, so a clause on its own line never reaches the phone.
    """
    missing = [str(c.get("command") or "").strip() for c in commands if c.get("unavailable")]
    missing = [c for c in missing if c]
    if not missing:
        return ""
    listed = " · ".join(missing)
    if ko:
        return f" 여기서 못 돌린 검사 {len(missing)}개(도구 없음): {listed}."
    noun = "check" if len(missing) == 1 else "checks"
    return f" {len(missing)} {noun} could not run here (tool missing): {listed}."


def _gate_command_sentence(passed: list[Any], commands: list[Any], ko: bool) -> str:
    """Single sentence summarising how many gate/declared commands passed — and,
    when some could not run at all, which those were (:func:`_could_not_run_clause`)."""
    could_not_run = _could_not_run_clause(commands, ko)
    if ko:
        return f"검증: {len(passed)}개 확인 통과.{could_not_run}"
    labels: list[str] = []
    for cmd in commands:
        text = str(cmd.get("command") or "").lower()
        for needle, label in _CHECK_CATEGORY_LABELS:
            if needle in text and label not in labels:
                labels.append(label)
    noun = "check" if len(passed) == 1 else "checks"
    sentence = f"Verified: {len(passed)} {noun} passed"
    if labels:
        sentence += f" ({', '.join(labels)})"
    return sentence + "." + could_not_run


def _probe_sentence(matched_probes: list[Any], ko: bool) -> str:
    """Sentence fragment for matched outcome-demonstration probes."""
    if not matched_probes:
        return ""
    if ko:
        return f"결과 시연됨 ({len(matched_probes)}개 프로브)."
    probe_noun = "probe" if len(matched_probes) == 1 else "probes"
    return f"Outcome demonstrated ({len(matched_probes)} {probe_noun})."


def _verification_sentence(result: Mapping[str, Any] | None, language: str = "en") -> str:
    """A deterministic, LLM-free sentence describing what the verifier proved.

    Reads the ``VerificationResult.result`` blob the verifier already persisted
    (``command_results`` + ``judge``, or the in-place ``derived_gate``) and
    renders e.g. "Verified: 3 checks passed (tests, lint, format). Acceptance
    check passed." (EN) or "검증: 3개 확인 통과. 검증 통과." (KO). Returns "" when
    there is no verdict / nothing to report, so the caller adds no empty line.
    Localized so a KO founder never sees the English verification chrome in the
    delivered summary.
    """
    if result is None:
        return ""
    # derived_gate is authoritative when present (repo manifests drove it);
    # command_results is advisory once a gate ran. Prefer gate commands so the
    # count reflects what actually decided the verdict, not what the agent declared.
    commands = _gate_command_results(result) or result.get("command_results") or []
    passed = [c for c in commands if c.get("passed")]
    ko = language == "ko"

    pieces: list[str] = []
    if passed:
        pieces.append(_gate_command_sentence(passed, commands, ko))

    # Count matched demonstration probes: they are independent behavioral checks
    # that actually ran in the sandbox and are not covered by the gate count above.
    demonstration = result.get("outcome_demonstration") or {}
    matched_probes = [
        p
        for p in (demonstration.get("probes") or [])
        if isinstance(p, dict) and p.get("status") == "matched"
    ]
    probe_s = _probe_sentence(matched_probes, ko)
    if probe_s:
        pieces.append(probe_s)

    weak = _weak_evidence_sentence(result, ko)
    judge = result.get("judge") or {}
    if judge.get("passed") and not weak:
        # SUPPRESSED under weak evidence, deliberately. Live run 6565db96 put
        # "검증 통과. 검증: 돌릴 검사가 없어 내용만 확인했어요 (증거 약함)." on the
        # founder's phone: both halves true, the pair reading as a claim and its
        # own retraction — with the strong half first, which is the half a glance
        # picks up. The judge passing IS "내용만 확인했어요", so the weak sentence
        # already says it, accurately and once.
        pieces.append("검증 통과." if ko else "Acceptance check passed.")
    if weak:
        pieces.append(weak)
    return " ".join(pieces)


def _weak_evidence_sentence(result: Mapping[str, Any], ko: bool) -> str:
    """Say it out loud when a verified run rests on nothing that actually ran.

    A weak finish must still REACH the founder: the push they receive is built
    from THIS line (``_shipped_detail`` lifts it by prefix), so a weak finish
    that never reaches it is one the founder never hears about (lesson #742). It
    stopped BLOCKING (검증은 통과/실패 둘 뿐 — 형님 판정 2026-08-20); it must not
    stop being SAID.

    Read from the persisted FACTS, never a grade letter — the retired A–D ladder
    was a second representation over exactly these. ``gate_applicable`` false (a
    Direct / non-worktree scratch answer) has no repo-gate concept to be weak
    about and stays silent, exactly as the ladder's ``None`` grade did. The two
    weak cases are different facts and must not share a sentence; both keep the
    ``검증``/``Verified`` prefix or the lifter drops them."""
    if not result.get("gate_applicable"):
        return ""
    commands = (result.get("derived_gate") or {}).get("commands") or []
    gate_passed = bool((result.get("derived_gate") or {}).get("passed")) and any(
        isinstance(c, dict) and c.get("status") == "passed" for c in commands
    )
    if gate_passed or (result.get("outcome_demonstration") or {}).get("verdict") == "demonstrated":
        return ""  # a real leg carried it — not weak
    if commands:  # a gate EXISTED here and none of it could run
        return (
            "검증: 검사를 찾았지만 여기서 돌릴 수 없었어요 (증거 약함)."
            if ko
            else "Verified: a gate was found but could not run here (weak evidence)."
        )
    return (
        "검증: 돌릴 검사가 없어 내용만 확인했어요 (증거 약함)."
        if ko
        else "Verified: by content only — no runnable check existed (weak evidence)."
    )


async def _changed_paths_for(run: ExecutionRun) -> list[str] | None:
    """The run's real changed-path list, or ``None`` when unknown.

    Same ref range and same best-effort contract as the deliverable's diff
    capture (:func:`~backend.storage.product_workspace.capture_run_changed_paths`).
    Product runs only — a non-product run has no worktree to diff, which is
    "unknown", not "changed nothing". The import is LAZY for the same reason the
    diff capture's is: this module must not pull the storage layer at import.
    """
    product_id = getattr(run, "product_id", None)
    if product_id is None:
        return None
    from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy, cross-layer
        capture_run_changed_paths,
    )

    try:
        return await capture_run_changed_paths(product_id, run.id)
    except Exception:  # noqa: BLE001 — grounding, never a broken verified terminal
        return None


def _compose_verified_summary(
    run: ExecutionRun,
    final_text: str,
    written_paths: Sequence[str] | None = None,
    verdict_result: Mapping[str, Any] | None = None,
    language: str = "en",
    changed_paths: Sequence[str] | None = None,
) -> str:
    """Build the verified deliverable's summary — titled by the founder INTENT,
    bodied by the DETERMINISTIC list of changed files + what the verifier proved.

    ``changed_paths`` is the run's REAL durable change list (``git diff
    --name-only main...HEAD``), the same source the deliverable's diff comes
    from. ``written_paths`` is every path the agent WROTE during the run, which
    is a SUPERSET: it keeps a scratch file the agent created and then deleted,
    and a file it opened but left byte-identical. Measured (prod run
    ``02af81f7``, 2026-08-26): the summary said "바뀐 파일 4개" while the PR it
    produced (#827) carried 2. That line becomes the PR title AND the settle
    note title, so the wrong count is written into the knowledge graph and
    injected into later runs as what happened.

    So the label names its own SOURCE, and never over-claims:

    * ``changed_paths`` given  → "Changed files:" / "바뀐 파일 N개:"
    * ``changed_paths`` is None (diff capture is best-effort — a cleaned
      worktree / non-product run) → the written paths under "Files touched:" /
      "건드린 파일 N개:", which is what they actually are.
    * ``changed_paths`` empty  → the run changed nothing durable. That is an
      ANSWER, not a missing value; falling back to the written paths there
      would restate exactly the falsehood this avoids.

    The summary's first line becomes the PR title (via ``_split_summary``) and
    the settle note's title. The work LLM's ``final_text`` is raw first-person
    streaming narration ("I'll invoke /feature-workflow… Now the
    implementation… Phase 1 (RED)…") with chunk-join whitespace artifacts plus
    the E30 contract block — slop in a user-facing deliverable summary / PR body
    (live dogfood F4; earlier garbage PR titles, PR #374). So lead with the
    founder intent (what was asked == what shipped for a verified run) and list
    what actually changed; the agent's prose stays in the ``llm_turn`` activity
    for debugging. ``final_text`` is only a FALLBACK body (contract-stripped,
    whitespace-repaired) when no changed-file list is available — e.g. a
    non-file deliverable. Falls back to a stable title when there is no intent.

    R1: when a passing verdict blob is supplied, a deterministic verification
    sentence (which checks passed, by category) is appended so the report says
    not just *what* changed but *that it was proven* — no LLM, no raw commands.
    Takes the ``VerificationResult.result`` MAPPING rather than the row: the
    in-place gate's proof (#692) is real evidence but is not the row the sandbox
    path holds, and only the blob was ever read here.
    """
    payload = run.payload or {}
    # Title in a formal, declarative register — a REPORT, not the founder's raw
    # imperative Direction ("dedup 함수를 추가해줘"). The FrameStage already produces
    # a SHORT plain-language, workspace-language ``summary_title`` ("dedup 유틸리티
    # 추가"); prefer it. Fall back to the intent first-line, then a stable string.
    frame = payload.get("frame")
    summary_title = str(
        (frame.get("summary_title") if isinstance(frame, dict) else "") or ""
    ).strip()
    intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
    first_line = next((ln.strip() for ln in intent.splitlines() if ln.strip()), "")
    title = (summary_title or first_line)[:_MAX_SUMMARY_TITLE].rstrip() or "Delivered change"

    known_changed = changed_paths is not None
    source = changed_paths if known_changed else written_paths
    files = [p.strip() for p in (source or []) if p and p.strip()]
    sections: list[str] = []
    if files:
        if known_changed:
            header = f"바뀐 파일 {len(files)}개:" if language == "ko" else "Changed files:"
        else:
            header = f"건드린 파일 {len(files)}개:" if language == "ko" else "Files touched:"
        sections.append(header + "\n" + "\n".join(f"- {p}" for p in files))
    else:
        stripped = _CONTRACT_BLOCK_RE.sub("", final_text or "").strip()
        cleaned = _CHUNK_JOIN_RE.sub(r"\1 \2", stripped)
        if cleaned:
            sections.append(cleaned)

    verification = _verification_sentence(verdict_result, language)
    if verification:
        sections.append(verification)

    body = "\n\n".join(sections)
    return f"{title}\n\n{body}" if body else title
