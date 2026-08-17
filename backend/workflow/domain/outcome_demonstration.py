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

Best-effort (founder decision #1): a deliverable that cannot be exercised yields
NO probes → verdict ``undemonstrable`` → the honesty grade downgrades, it does
NOT fail. Only a probe that RAN and *contradicted* its declared expectation
fails verification.

**Two surfaces, two groundings** (design SoT §8). A CODE deliverable is probed
by CALLING it, so its planner reads the source (it needs the API, and the
machine's answer is what decides — reading cannot fake that). A produced
ARTIFACT — a report, a dataset, a document — is probed by INSPECTING it, and
whoever holds the text can always write a grep that finds it. So the artifact
planner is grounded in the TASK and the file PATHS only
(:func:`artifact_planner_messages`), and because it declares expectations about
wording it never saw, its fold is ADVISORY: it can EARN the demonstrated leg,
never manufacture a failure (``summarize(..., contradiction_fails=False)``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Cap the plan so a runaway verifier cannot balloon verify time.
MAX_PROBES = 6
MAX_SETUP = 4

ProbeStatus = Literal["matched", "contradicted", "unavailable", "not_seen"]
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
    #: True when the planner was given truncated source for this file.
    #: A contradiction from a truncated-source probe is ``not_seen`` (the
    #: planner's blind spot) rather than a genuine deliverable defect —
    #: ``judge_probe`` returns ``"not_seen"`` instead of ``"contradicted"``
    #: so ``summarize`` downgrades to ``undemonstrable`` rather than failing.
    source_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "expect_exit_zero": self.expect_exit_zero,
            "expect_stdout_contains": list(self.expect_stdout_contains),
        }
        if self.source_truncated:
            d["source_truncated"] = True
        return d


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
    """A probe judged against its observation.

    Accepts two construction styles for backward compatibility:

    **Flat (new)** — callers pass ``stdout``, ``stderr``, ``exit_code`` and
    ``timed_out`` directly as top-level fields.  Used by probe tests and new
    internal code::

        ProbeResult(probe=p, status="matched", stdout="42\n", exit_code=0)

    **Nested (legacy)** — callers pass an ``Observation`` object.  All existing
    code that already does ``ProbeResult(probe=p, observation=obs, status=s)``
    continues to work unchanged because ``observation`` accepts a keyword arg::

        ProbeResult(probe=probe, observation=obs, status=judge_probe(probe, obs))
    """

    probe: Probe
    status: ProbeStatus
    # Legacy nested struct — kept so existing callers compile without change.
    # ``to_dict`` prefers flat fields when ``observation`` is absent.
    observation: Observation | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.observation is not None:
            stdout = self.observation.stdout
            stderr = self.observation.stderr
            exit_code = self.observation.exit_code
            timed_out = self.observation.timed_out
        else:
            stdout = self.stdout
            stderr = self.stderr
            exit_code = self.exit_code
            timed_out = self.timed_out
        tail = "\n".join(o for o in (stdout, stderr) if o)[-2000:]
        return {
            "name": self.probe.name,
            "command": self.probe.command,
            "expect_exit_zero": self.probe.expect_exit_zero,
            "expect_stdout_contains": list(self.probe.expect_stdout_contains),
            "exit_code": exit_code,
            "timed_out": timed_out,
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
    ``not_seen`` — the probe was planned from truncated source; a mismatch may
    be the planner’s blind spot, not a deliverable defect. Treated as
    ``unavailable`` by ``summarize`` (downgrade, never fail).
    """
    if obs.timed_out or obs.exit_code is None:
        return "unavailable"
    combined = f"{obs.stdout}\n{obs.stderr}"
    if obs.exit_code == 127 or any(m in combined for m in _UNAVAILABLE_MARKERS):
        return "unavailable"
    exit_ok = (obs.exit_code == 0) == probe.expect_exit_zero
    stdout_ok = all(s in combined for s in probe.expect_stdout_contains)
    if exit_ok and stdout_ok:
        return "matched"
    # source_truncated=True: the planner authored this probe from incomplete
    # source (file was cut at the read cap). Its expectations may target code
    # it never saw, so a mismatch means "planner couldn't verify" (not_seen),
    # NOT "deliverable is broken" (contradicted). summarize treats not_seen
    # the same as unavailable — downgrade to undemonstrable, never fail.
    return "not_seen" if probe.source_truncated else "contradicted"


def summarize(
    plan_or_results: Any,
    results: Any = None,
    *,
    contradiction_fails: bool = True,
) -> DemonstrationVerdict:
    """Fold probe results into one demonstration verdict.

    Accepts two calling styles:

    **Classic** (``results`` only)::

        summarize([r1, r2])
        summarize(results, contradiction_fails=False)

    **Plan-first** (for probe tests that need the plan context)::

        summarize(plan, [r1, r2])

    The plan is accepted but currently ignored in the verdict; the verdict is
    determined solely by the statuses in ``results``.

    ANY ``contradicted`` ⇒ ``failed``.  At least one ``matched`` ⇒
    ``demonstrated``.  All others (``unavailable``, ``not_seen``, empty) ⇒
    ``undemonstrable`` (best-effort downgrade, never a fail).

    ``contradiction_fails=False`` — the ADVISORY fold used for the artifact
    surface, where the planner guessed at wording it never saw. A miss there
    downgrades to ``undemonstrable`` instead of failing."""
    if isinstance(plan_or_results, DemonstrationPlan):
        actual: list[ProbeResult] = list(results) if results is not None else []
    else:
        actual = list(plan_or_results)
    statuses = [r.status for r in actual]
    if contradiction_fails and "contradicted" in statuses:
        return "failed"
    if "matched" in statuses:
        return "demonstrated"
    return "undemonstrable"


def drop_unasserted(plan: DemonstrationPlan) -> DemonstrationPlan:
    """Keep only probes that DECLARED an observation — the rest are not evidence.

    A probe whose only expectation is "exit 0" is evidence *if the command can
    fail*. Nothing checks that, and the blind artifact planner reliably writes
    commands that cannot: live run e72689e8 (2026-08-14) produced
    ``python -c "…; print('found' if ex else 'missing')"`` — it COMPUTED the
    answer, printed it, and declared nothing, so Python exited 0 either way and
    the probe scored ``matched`` no matter what the artifact said. Two of that
    run's six probes were unfalsifiable and the grade could not tell.

    So a declared expectation is required: either a substring that must appear,
    or ``expect_exit_zero=False`` (a command that must FAIL is falsifiable — it
    is contradicted by succeeding). ``setup`` is untouched: it is preparation,
    never asserted.

    Dropping can only WEAKEN a verdict (fewer probes → at most ``undemonstrable``),
    so it cannot open a hole; it closes one."""
    kept = tuple(p for p in plan.probes if p.expect_stdout_contains or not p.expect_exit_zero)
    return DemonstrationPlan(probes=kept, setup=plan.setup)


# ── Grounding for the ARTIFACT surface (prose / data deliverables) ────────────


#: Deliberately BLIND to the deliverable's text — see the function docstring.
_ARTIFACT_SYSTEM_PROMPT = (
    "You are an INDEPENDENT outcome-demonstration verifier. The deliverable under "
    "test is NOT code you can call — it is a produced ARTIFACT (a document, a "
    "report, a dataset, a config). You design executable PROBES that INSPECT the "
    "produced files and OBSERVE whether what the TASK required is actually there.\n"
    "You have NOT been shown the artifact's contents, and that is deliberate: an "
    "expectation copied out of the text you are grading is satisfied by any text "
    "at all and proves nothing. Derive EVERY expectation from the TASK alone.\n"
    "For each probe output:\n"
    '  - "name": what outcome it demonstrates\n'
    '  - "command": ONE line of shell that inspects a produced file and prints an '
    "observable result (grep for a required element, count rows, parse the file "
    "and print a field). For Python use `python -c` with statements joined by ';' "
    "(NOT literal \\n — inside `python -c` a backslash-n is not a newline and "
    "raises SyntaxError).\n"
    '  - "expect_stdout_contains": REQUIRED — the exact substring(s) that MUST '
    "appear if the TASK was carried out. A probe that declares nothing cannot "
    "fail and is discarded: a command that computes the answer and PRINTS it "
    "still exits 0 when the answer is bad (`python -c \"print('found' if x else "
    "'missing')\"` exits 0 either way), so exit status alone proves nothing "
    "there. Print the finding AND declare the substring that must be in it. The "
    "only exception is a probe that must FAIL, which declares that instead.\n"
    '  - "expect_exit_zero": true if the command must succeed, false if it must fail\n'
    "RULES:\n"
    "- Assert only what the TASK UNAMBIGUOUSLY requires — a named figure, a "
    "required section, a row count, a declared format. NEVER assert particular "
    "PHRASING or wording the author was free to choose; you never saw the text, "
    "and a guess that misses wrongly withholds credit from good work.\n"
    "- Prefer a requirement that is checkable against something OUTSIDE the "
    "artifact (a source file, a tool that regenerates the value) over anything "
    "the artifact alone asserts about itself.\n"
    "- Every probe runs from the REPO ROOT (the deliverable's checkout is your "
    "current working directory). Use ONLY paths RELATIVE to it. NEVER `cd` and "
    "NEVER use an absolute filesystem path — that location will NOT exist when "
    "the probe runs.\n"
    "- If the TASK states no requirement an executable probe can observe, return "
    "an empty probes list — that is a valid, honest answer.\n"
    '- Output ONLY a JSON object: {"setup": [...], "probes": [ {...} ]}. No prose.'
)


def artifact_planner_messages(*, intent: str, artifact_paths: list[str]) -> list[dict[str, str]]:
    """Ground the planner in the TASK and the artifact's PATHS — never its text.

    The code path (``_demonstration_planner_messages`` in the verification
    service) shows the planner the source, because writing a probe that CALLS
    the deliverable takes knowing its API, and the machine's answer is what
    decides — reading the source cannot fake that. Inspecting a document is the
    opposite: whoever holds the text can always write a grep that finds it. So
    the artifact surface withholds the text and keeps the task (design SoT §8.3
    / §4 rule 1)."""
    listing = "\n".join(f"- {p}" for p in artifact_paths)
    user = f"TASK (the intended result):\n{intent}\n\nPRODUCED artifact files:\n{listing}"
    return [
        {"role": "system", "content": _ARTIFACT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


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
    "artifact_planner_messages",
    "drop_unasserted",
    "judge_probe",
    "parse_demonstration_plan",
    "summarize",
]
