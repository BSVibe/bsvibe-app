"""Outcome demonstration — invariant **I2**, the "half judge".

The core failure this closes (findings 2026-07-01, Q-2): the old independent
acceptance check authored ONE pytest from the intent and checked
intent-satisfaction — a pure judgement that let *garbage* pass "verified" (an
executor that edited 12 spurious files for a "add one README line" task still
satisfied a loosely-authored intent test).

I2 replaces that with a **demonstration**: an independent verifier PLANS how to
*exercise the finished deliverable* and declares, for each probe, the **literal
observation** that MUST appear if the intended result actually happens. The
harness runs the probe and the verdict is a **pure, deterministic comparison**
``observation == expectation`` — no LLM sits in the verdict loop, so the
half-judge cannot collapse back into "the model felt it was fine" (§2 of the
redesign SoT).

This module owns the stack-agnostic *schema + verdict*: parse an LLM-authored
plan, judge one probe against one observation, and summarize a plan's probe
results into a single demonstration verdict. It is pure and offline — the LLM
call and the sandbox execution live in the verification service.

Best-effort (founder decision #1): a deliverable that cannot be exercised (pure
prose / half-built code) yields NO probes → verdict ``undemonstrable`` → the
honesty grade downgrades, it does NOT fail. Only a probe that RAN and
*contradicted* its declared expectation fails verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Cap the plan so a runaway verifier cannot balloon verify time.
MAX_PROBES = 6
MAX_SETUP = 4

ProbeStatus = Literal["matched", "contradicted", "unavailable"]
DemonstrationVerdict = Literal["demonstrated", "failed", "undemonstrable"]

#: Substrings in a probe's combined output that mark it as UNABLE to exercise
#: the deliverable — a missing interpreter/command, a wrong import, or a probe
#: COMMAND that didn't even parse/run — rather than a genuine contradiction of
#: the intended result. These are the verifier's/environment's fault, not the
#: deliverable's, so they downgrade (unavailable) instead of false-failing good
#: code. A real source defect that breaks import/parse is still caught by
#: invariant I1 (the repo's own lint/type gate), so nothing slips through.
#:
#: The parse/usage markers were added after L-measure (2026-07-02): a live run
#: whose factorial code was CORRECT (probe 1 matched: factorial(5)=120) got a
#: FALSE fail because the verifier wrote a second probe as
#: ``python -c "…\ntry:\n…"`` — literal ``\n`` inside a ``python -c`` string is
#: not a newline, so the command died with a SyntaxError. A probe that can't be
#: parsed never exercised the deliverable → unavailable, never contradicted.
_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    # missing interpreter / command / module (probe couldn't start)
    "command not found",
    "No such file or directory",
    "ModuleNotFoundError",
    "ImportError",
    "No module named",
    "cannot find module",
    "Cannot find module",
    "is not recognized as",
    # the probe COMMAND itself failed to parse / was mis-authored (verifier's
    # fault) — it never ran the deliverable, so it cannot contradict it.
    "SyntaxError",
    "invalid syntax",
    "unexpected character after line continuation",
    "unexpected EOF while parsing",
    "IndentationError",
    "unexpected token",  # shell parse error (bash/sh)
    "syntax error near",  # shell parse error
    # the probe tried to `cd` into a directory that isn't there at verify time —
    # e.g. a planner (a claude_code CLI account) that hard-coded its OWN host
    # workdir, which is GONE by verify (verify runs in a fresh clone). The probe
    # never reached the deliverable → unavailable, not a contradiction. Both the
    # dash ("can't cd to") and bash ("cd: <p>: No such file or directory", caught
    # above) phrasings must downgrade.
    "can't cd to",
    "cannot cd to",
)


@dataclass(frozen=True)
class Probe:
    """One executable demonstration: run ``command`` against the finished
    deliverable and assert the declared observation.

    ``expect_exit_zero`` — the command must exit 0 (True) or non-zero (False,
    e.g. "the CLI rejects invalid input"). ``expect_stdout_contains`` — every
    listed substring must appear in the observed output (stdout or stderr). A
    probe with neither a meaningful command nor any expectation is not a
    demonstration and is dropped by the parser."""

    name: str
    command: str
    expect_exit_zero: bool = True
    expect_stdout_contains: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "expect_exit_zero": self.expect_exit_zero,
            "expect_stdout_contains": list(self.expect_stdout_contains),
        }


@dataclass(frozen=True)
class DemonstrationPlan:
    """An independent verifier's plan to exercise the deliverable.

    ``setup`` commands prepare the environment (build, install) and are NOT
    asserted — a failed setup just makes the affected probes unavailable.
    ``probes`` are the asserted demonstrations."""

    probes: tuple[Probe, ...] = ()
    setup: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.probes

    def to_dict(self) -> dict[str, Any]:
        return {"setup": list(self.setup), "probes": [p.to_dict() for p in self.probes]}


@dataclass(frozen=True)
class Observation:
    """What running a probe produced. ``exit_code`` is ``None`` when the
    command was killed before it exited (a timeout)."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class ProbeResult:
    """A probe judged against its observation."""

    probe: Probe
    observation: Observation
    status: ProbeStatus

    def to_dict(self) -> dict[str, Any]:
        obs = self.observation
        tail = "\n".join(o for o in (obs.stdout, obs.stderr) if o)[-2000:]
        return {
            "name": self.probe.name,
            "command": self.probe.command,
            "expect_exit_zero": self.probe.expect_exit_zero,
            "expect_stdout_contains": list(self.probe.expect_stdout_contains),
            "exit_code": obs.exit_code,
            "timed_out": obs.timed_out,
            "status": self.status,
            "output": tail,
        }


@dataclass(frozen=True)
class DemonstrationOutcome:
    """The whole plan judged: per-probe results + one verdict."""

    verdict: DemonstrationVerdict
    results: tuple[ProbeResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "probes": [r.to_dict() for r in self.results]}


# ── Parsing (tolerant, mirrors verifier_contract) ────────────────────────────


def _as_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _parse_probe(raw: Any) -> Probe | None:
    if not isinstance(raw, dict):
        return None
    command = str(raw.get("command") or raw.get("cmd") or "").strip()
    if not command:
        return None
    name = str(raw.get("name") or raw.get("label") or command).strip()[:200]
    # Default True — a probe that omits the exit expectation still asserts the
    # command succeeds, which is the common "exercise it and it works" case.
    exit_raw = raw.get("expect_exit_zero")
    if exit_raw is None:
        exit_raw = raw.get("exit_zero")
    expect_exit_zero = True if exit_raw is None else bool(exit_raw)
    contains = _as_str_list(
        raw.get("expect_stdout_contains")
        or raw.get("stdout_contains")
        or raw.get("contains")
        or raw.get("expect_output")
    )
    return Probe(
        name=name,
        command=command,
        expect_exit_zero=expect_exit_zero,
        expect_stdout_contains=tuple(contains),
    )


def parse_demonstration_plan(raw: Any) -> DemonstrationPlan:
    """Parse an LLM-authored plan. Tolerant: invalid probes are dropped. An
    absent/empty probe list yields an empty plan (→ ``undemonstrable``), never
    an error — an undemonstrable deliverable is a valid, honest outcome."""
    if not isinstance(raw, dict):
        return DemonstrationPlan()
    probes = [p for p in (_parse_probe(item) for item in _as_list(raw.get("probes"))) if p][
        :MAX_PROBES
    ]
    setup = _as_str_list(raw.get("setup"))[:MAX_SETUP]
    return DemonstrationPlan(probes=tuple(probes), setup=tuple(setup))


def _as_list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


# ── Verdict (pure, deterministic — the anti-collapse core) ────────────────────


def judge_probe(probe: Probe, obs: Observation) -> ProbeStatus:
    """Judge ONE probe against ONE observation — deterministically, with NO
    model in the loop. This is what keeps the half-judge honest.

    ``unavailable`` — the probe could not exercise the deliverable (timeout,
    missing command/interpreter, or a wrong import path). Not a code defect →
    never a false-fail. ``matched`` — the declared observation was seen.
    ``contradicted`` — the probe ran and the intended result was NOT observed.
    """
    if obs.timed_out or obs.exit_code is None:
        return "unavailable"
    combined = f"{obs.stdout}\n{obs.stderr}"
    if obs.exit_code == 127 or any(m in combined for m in _UNAVAILABLE_MARKERS):
        return "unavailable"
    exit_ok = (obs.exit_code == 0) == probe.expect_exit_zero
    stdout_ok = all(s in combined for s in probe.expect_stdout_contains)
    return "matched" if (exit_ok and stdout_ok) else "contradicted"


def summarize(results: list[ProbeResult] | tuple[ProbeResult, ...]) -> DemonstrationVerdict:
    """Fold probe results into one demonstration verdict.

    ANY contradiction ⇒ ``failed`` (the deliverable was exercised and did not
    produce the intended result). Otherwise, at least one ``matched`` ⇒
    ``demonstrated``. No matches (empty plan or all unavailable) ⇒
    ``undemonstrable`` (best-effort: downgrade, do not fail)."""
    statuses = [r.status for r in results]
    if "contradicted" in statuses:
        return "failed"
    if "matched" in statuses:
        return "demonstrated"
    return "undemonstrable"


__all__ = [
    "MAX_PROBES",
    "MAX_SETUP",
    "DemonstrationOutcome",
    "DemonstrationPlan",
    "DemonstrationVerdict",
    "Observation",
    "Probe",
    "ProbeResult",
    "ProbeStatus",
    "judge_probe",
    "parse_demonstration_plan",
    "summarize",
]
