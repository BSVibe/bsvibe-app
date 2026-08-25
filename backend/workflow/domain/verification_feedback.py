"""Render a FAILED verification back to the agent, failure first.

The agent is the consumer here, and it only ever knows what this string tells
it. That makes the ordering load-bearing: an agent that cannot see the failure
does not go looking elsewhere the way a person would — it re-runs its own
checks, sees them pass, resubmits, and repeats until the round budget is gone.

Run ``010bbdd8`` did exactly that: 16 attempts, 41 minutes, three
byte-identical mypy errors, none of which ever reached it. The feedback was
``json.dumps(verdict.result)[:1500]``, and ``command_results`` — the agent's
own attestation, which ``verification_service`` explicitly treats as ADVISORY —
is serialised first and carried a verbose ``pytest -v`` log. The authoritative
``derived_gate`` failure sat at index 3516 of a 13,056-character blob. What the
agent actually received was "Verification FAILED. Details: [everything passed]".

So this module does not truncate a report; it *builds* one, out of the failures
alone, in authority order. Passing checks are summarised in a single line and
can never displace a failure. Two further properties the blind prefix lacked:

* Text is rendered as text, so a Korean judge reasoning costs one character per
  character instead of the six ``json.dumps`` spends on ``\\uXXXX`` escapes.
* Every clip says how much it dropped. A silent cap does not merely omit — under
  a "FAILED" headline it asserts something false.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Generous next to the 1500 that lost run 010bbdd8, but the budget is not what
# fixed this — the ordering is. A budget only decides how much of the tail of an
# already-correct report survives.
FEEDBACK_BUDGET = 6_000
_PER_OUTPUT_BUDGET = 2_000

NO_FAILURE_FOUND = (
    "Verification did not pass, but no specific check reported a failure. "
    "This points at the verification harness rather than your work — say so in "
    "your summary rather than changing code at random."
)

#: The harness itself could not produce the repo gate. ``verification_service``
#: fails CLOSED here on purpose (INV-2: a repo with a toolchain manifest must
#: not reach PROVED with zero objective gate commands run) — but the run failed
#: for a reason that is NOT about the agent's work, and the agent has to be told
#: which, or it will go looking for a defect in code that has none.
DERIVER_FAILED = (
    "The repo's verification gate could not be produced by the harness "
    "({reason}). This is a failure of the verification system, NOT of your "
    "work — every check you declared passed. Do not change your code and do "
    "not widen your declared checks to compensate; say what happened in your "
    "summary so a human can look at the harness."
)


def _clip(text: str, limit: int) -> str:
    """Keep both ends of *text* and state what was dropped.

    Both ends, because different tools put the answer in different places:
    mypy leads with its errors, pytest closes with its summary. A head-only
    clip loses whichever half the tool chose.
    """
    if len(text) <= limit:
        return text
    template = "\n… {n} characters omitted …\n"
    room = limit - len(template.format(n=len(text)))
    if room <= 0:
        return text[:limit]
    head = room * 2 // 3
    tail = room - head
    notice = template.format(n=len(text) - head - tail)
    return text[:head] + notice + (text[-tail:] if tail else "")


def _command_block(cmd: Mapping[str, Any], *, label: str) -> str:
    lines = [f"{label}: {cmd.get('command', '<unknown command>')}"]
    if cmd.get("timed_out"):
        lines.append("  → timed out")
    else:
        lines.append(f"  → exit code {cmd.get('exit_code')}")
    output = str(cmd.get("output") or "").strip()
    lines.append(_clip(output, _PER_OUTPUT_BUDGET) if output else "  (no output)")
    return "\n".join(lines)


def _failed(commands: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Commands that FAILED, by either vocabulary.

    ``derived_gate`` commands carry ``status``; the agent's own attestation
    carries ``passed``. A command that timed out failed regardless of what
    exit code the runner invented for it.
    """
    out = []
    for c in commands:
        if not isinstance(c, Mapping):
            continue
        if c.get("status") == "failed" or c.get("passed") is False or c.get("timed_out"):
            out.append(c)
    return out


def _probe_block(probe: Mapping[str, Any]) -> str:
    lines = [f"PROBE FAILED: {probe.get('name', '<unnamed>')}"]
    lines.append(f"  command: {probe.get('command', '<unknown>')}")
    expected = probe.get("expect_stdout_contains") or []
    if isinstance(expected, Sequence) and not isinstance(expected, str) and expected:
        lines.append(f"  expected stdout to contain: {list(expected)}")
    if probe.get("expect_exit_zero"):
        lines.append("  expected exit code: 0")
    if probe.get("timed_out"):
        lines.append("  → timed out")
    else:
        lines.append(f"  → exit code {probe.get('exit_code')} ({probe.get('status')})")
    output = str(probe.get("output") or "").strip()
    lines.append(_clip(output, _PER_OUTPUT_BUDGET) if output else "  (no output)")
    return "\n".join(lines)


def _sections(result: Mapping[str, Any]) -> list[str]:
    """Every failing surface, in the order ``verification_service`` ranks them."""
    sections: list[str] = []

    # 1. The repo's own derived gate. Authoritative whenever it ran, which is
    #    why it goes first and why nothing else may push it out of the budget.
    gate = result.get("derived_gate")
    if isinstance(gate, Mapping):
        for cmd in _failed(gate.get("commands") or []):
            sections.append(_command_block(cmd, label="GATE COMMAND FAILED"))
    else:
        # 1b. The gate did not merely fail — it could not be BUILT. Same
        #     authority slot, because it is the same verdict source, and it is
        #     the only surface that knows why this run failed: every other
        #     section below reads a check that PASSED. prod ``e6472fab`` hit
        #     exactly this and, told "no specific check reported a failure",
        #     spent six rounds widening its own gate until it grep-gated a file
        #     full of unrelated tests and had to ask a human.
        reason = result.get("gate_deriver_failed")
        if isinstance(reason, str) and reason:
            sections.append(DERIVER_FAILED.format(reason=reason))

    # 2. Outcome demonstration. Only a "failed" verdict gates —
    #    "undemonstrable" is weak evidence, not a defect to repair.
    demo = result.get("outcome_demonstration")
    if isinstance(demo, Mapping) and demo.get("verdict") == "failed":
        for probe in demo.get("probes") or []:
            if isinstance(probe, Mapping) and probe.get("status") != "matched":
                sections.append(_probe_block(probe))

    # 3. The judge, when it was gating rather than advisory.
    judge = result.get("judge")
    if isinstance(judge, Mapping) and judge.get("passed") is False:
        reasoning = str(judge.get("reasoning") or "(no reasoning given)")
        sections.append("JUDGE REJECTED THE WORK:\n" + _clip(reasoning, _PER_OUTPUT_BUDGET))

    # 4. Scope. Not gating on its own, but it names files the agent should not
    #    have touched, and an agent that cannot see them cannot put them back.
    scope = result.get("scope")
    if isinstance(scope, Mapping) and scope.get("flagged_paths"):
        lines = [f"SCOPE FLAGGED: {list(scope['flagged_paths'])}"]
        if scope.get("reasoning"):
            lines.append(_clip(str(scope["reasoning"]), _PER_OUTPUT_BUDGET))
        sections.append("\n".join(lines))

    # 5. The agent's own attestation, LAST — advisory once the derived gate has
    #    spoken, and the block whose verbosity used to bury everything above it.
    for cmd in _failed(result.get("command_results") or []):
        sections.append(_command_block(cmd, label="YOUR OWN DECLARED COMMAND FAILED"))

    return sections


def _assemble(sections: Sequence[str], passed_note: str, budget: int) -> str:
    if not sections:
        return NO_FAILURE_FOUND
    body = "\n\n".join(sections)
    if passed_note:
        body = f"{body}\n\n{passed_note}"
    return _clip(body, budget)


def render_verification_failure(result: Mapping[str, Any], *, budget: int = FEEDBACK_BUDGET) -> str:
    """The body of the "Verification FAILED" turn handed back to the agent."""
    sections = _sections(result)
    gate = result.get("derived_gate")
    gate_commands = gate.get("commands") or [] if isinstance(gate, Mapping) else []
    declared = result.get("command_results") or []
    total = len(gate_commands) + len(declared)
    failed = len(_failed(gate_commands)) + len(_failed(declared))
    note = ""
    if total and failed < total:
        note = f"({total - failed} other command(s) passed; their output is omitted.)"
    return _assemble(sections, note, budget)


def render_failed_commands(
    commands: Sequence[Mapping[str, Any]], *, budget: int = FEEDBACK_BUDGET
) -> str:
    """The same contract for the in-place gate, which reports commands only."""
    failed = _failed(commands)
    sections = [_command_block(c, label="GATE COMMAND FAILED") for c in failed]
    note = ""
    if commands and len(failed) < len(commands):
        note = f"({len(commands) - len(failed)} other command(s) passed; their output is omitted.)"
    return _assemble(sections, note, budget)
