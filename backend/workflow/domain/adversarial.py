"""The adversarial verifier's pure rules: what a finding is, where it may write, and how the
harness (never the model) decides a repro confirms it.

A finding counts only with a repro the harness can run. Confirmed = the repro ran on the
changed tree and showed the defect; anything else (passed, could not run, timed out) is
unconfirmed. Nothing here calls a model.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from dataclasses import dataclass
from typing import Any

ADVERSARIAL_KEY = "adversarial"
MAX_FINDINGS = 5
OUTPUT_CHARS = 1500
SEVERITIES = ("high", "medium", "low")
SURFACES = ("code", "artifact")
#: Directory (relative to the workspace) the harness owns; the only place the verifier writes.
SCRATCH_ROOT = ".bsvibe_adversarial"

_TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*", "*_test.go")
#: pytest: 1 = tests failed. 2+ = interrupted / usage error, 5 = nothing collected → NOT a failure.
_PYTEST_FAILED = 1
_JS_FAILED = 1


def write_refusal(rel_path: str, scratch_dir: str) -> str | None:
    """Why the verifier may not write ``rel_path`` (``None`` = allowed).

    Allowed: a test file directly inside the harness's scratch directory. Anything else — a
    source file, a path that escapes via ``..``, an absolute path — is refused."""
    if not rel_path or rel_path.startswith("/") or "\x00" in rel_path:
        return "절대 경로나 빈 경로에는 쓸 수 없다"
    clean = posixpath.normpath(rel_path)
    if clean.startswith("../") or clean == "..":
        return "작업 영역 밖에는 쓸 수 없다"
    if not clean.startswith(scratch_dir.rstrip("/") + "/"):
        return f"임시 테스트 디렉터리({scratch_dir}) 밖에는 쓸 수 없다"
    name = posixpath.basename(clean)
    if not any(fnmatch.fnmatch(name, p) for p in _TEST_FILE_PATTERNS):
        return "테스트 파일이 아닌 파일은 쓸 수 없다"
    return None


@dataclass(frozen=True)
class Repro:
    kind: str  # "test" | "command"
    path: str | None
    command: str | None
    expect: dict[str, Any]


EXPECTED_KINDS = ("request", "existing_test", "unchanged_contract")
REASON_NO_BASIS = "기대 근거 없음"
REASON_BASIS_MISMATCH = "기대 근거 불일치"
REASON_INTENT_CONFLICT = "요청된 변경과 충돌(의도된 동작)"
REASON_INTENT_NO_TOKENS = "토큰 예산 소진으로 의도 충돌 판정을 하지 못했다"
#: A `request` quote shorter than this (whitespace-normalised) is not evidence of anything.
MIN_QUOTE_CHARS = 20
MIN_QUOTE_WORDS = 3


@dataclass(frozen=True)
class ExpectedFrom:
    """Where the repro's 'correct behaviour' comes from — the finding's stated basis.

    ``request``: ``quote`` (verbatim) and optionally the ledger ``req_id``.
    ``existing_test``: ``test`` = ``path::name`` of a test that passed before the change.
    ``unchanged_contract``: ``location`` = ``file:line`` of the code BEFORE the change."""

    kind: str
    quote: str = ""
    req_id: str = ""
    test: str = ""
    location: str = ""


@dataclass(frozen=True)
class Finding:
    severity: str
    claim: str
    file: str | None
    line: int | None
    repro: Repro
    expected_from: ExpectedFrom | None = None


def _text(raw: Any, limit: int = 600) -> str:
    return raw.strip()[:limit] if isinstance(raw, str) else ""


def parse_expected_from(raw: Any) -> ExpectedFrom | None:
    """``None`` for anything that does not name a complete basis (→ 'no basis')."""
    if not isinstance(raw, dict) or raw.get("kind") not in EXPECTED_KINDS:
        return None
    ef = ExpectedFrom(
        kind=raw["kind"],
        quote=_text(raw.get("quote"), 1000),
        req_id=_text(raw.get("req_id") or raw.get("id"), 80),
        test=_text(raw.get("test")),
        location=_text(raw.get("location")),
    )
    needed = {"request": ef.quote, "existing_test": ef.test, "unchanged_contract": ef.location}
    return ef if needed[ef.kind] else None


def normalize_ws(text: str) -> str:
    """Whitespace collapsed to single spaces (the quote check ignores line breaks / indents)."""
    return " ".join((text or "").split())


_LOCATION = re.compile(r"^[^\s:]+:\d+(?:-\d+)?$")


def split_test_id(test: str) -> tuple[str, str] | None:
    """``path::name`` → (path, name); ``None`` when it is not that shape."""
    path, sep, name = test.partition("::")
    if not sep or not path.strip() or not name.strip() or path.startswith("/") or ".." in path:
        return None
    return path.strip(), name.strip()


def check_expected_basis(  # noqa: PLR0911 — one return per outcome, flat on purpose
    ef: ExpectedFrom | None, requests: list[tuple[str, str]]
) -> str | None:
    """The mechanical part of the basis check that needs no run (``None`` = passes so far).

    ``requests`` = (id, text) pairs: the request body (id ``""``) and each ledger item."""
    if ef is None:
        return REASON_NO_BASIS
    if ef.kind == "request":
        quote = normalize_ws(ef.quote)
        if len(quote) < MIN_QUOTE_CHARS or len(quote.split()) < MIN_QUOTE_WORDS:
            # A one-letter or common-word 'quote' is found in any text: it proves nothing.
            return (
                f"{REASON_BASIS_MISMATCH} (인용이 너무 짧다: 공백 정규화 후 "
                f"{MIN_QUOTE_CHARS}자·{MIN_QUOTE_WORDS}단어 이상이어야 한다)"
            )
        pool = [t for i, t in requests if not ef.req_id or i in ("", ef.req_id)]
        if quote and any(quote in normalize_ws(t) for t in pool):
            return None
        return f"{REASON_BASIS_MISMATCH} (인용이 요청·장부 본문에 없다)"
    if ef.kind == "existing_test":
        if split_test_id(ef.test) is None:
            return f"{REASON_BASIS_MISMATCH} (기존 테스트는 경로::이름 형식이어야 한다)"
        return None
    if not _LOCATION.match(ef.location) or ef.location.startswith("/"):
        return f"{REASON_BASIS_MISMATCH} (변경 전 코드 위치는 파일:줄 형식이어야 한다)"
    return None


def _expect(raw: Any) -> dict[str, Any] | None:
    """Normalise ``expect``: ``{"exit_code": int|"nonzero", "contains": str, "not_contains": str}``.

    A bare string is read as a substring the output must contain."""
    if isinstance(raw, str) and raw.strip():
        return {"contains": raw}
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    code = raw.get("exit_code")
    if code == "nonzero" or (isinstance(code, int) and not isinstance(code, bool)):
        out["exit_code"] = code
    for key in ("contains", "not_contains"):
        if isinstance(raw.get(key), str) and raw[key]:
            out[key] = raw[key]
    return out or None


def _parse_repro(raw: Any) -> Repro | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "test":
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        return Repro("test", path.strip(), None, {})
    if kind == "command":
        command = raw.get("command")
        expect = _expect(raw.get("expect"))
        if not isinstance(command, str) or not command.strip() or expect is None:
            return None
        return Repro("command", None, command.strip(), expect)
    return None


def _load_json(raw: str) -> Any:
    """The JSON value inside a model reply (bare, fenced, or embedded in prose); ``None`` if none."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None


@dataclass(frozen=True)
class ToolRequest:
    """One tool the model asked the HARNESS to run (in the copy)."""

    name: str
    args: dict[str, Any]


def parse_reply(raw: str) -> ToolRequest | list[Finding] | None:
    """A verifier turn \u2192 a tool request, the final findings, or ``None`` (malformed).

    The model never calls tools itself: it answers ``{"tool": ..., "args": {...}}`` or
    ``{"findings": [...]}`` as plain text, so this works the same on any chat backend."""
    data = _load_json(raw)
    if not isinstance(data, dict):
        return None
    if "findings" in data:
        return parse_findings(raw)
    name, args = data.get("tool"), data.get("args", {})
    if name in TOOL_NAMES and isinstance(args, dict):
        return ToolRequest(name=name, args=args)
    return None


def parse_findings(raw: str) -> list[Finding] | None:
    """The verifier's final message → findings (≤ ``MAX_FINDINGS``); a finding without a
    runnable repro is dropped. ``None`` = the output was not the expected JSON at all."""
    data = _load_json(raw)
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        repro = _parse_repro(item.get("repro"))
        claim = item.get("claim")
        if repro is None or not isinstance(claim, str) or not claim.strip():
            continue
        severity = item.get("severity")
        line = item.get("line")
        out.append(
            Finding(
                severity=severity if severity in SEVERITIES else "medium",
                claim=claim.strip()[:400],
                file=item["file"] if isinstance(item.get("file"), str) else None,
                line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                repro=repro,
                expected_from=parse_expected_from(item.get("expected_from")),
            )
        )
        if len(out) == MAX_FINDINGS:
            break
    return out


def repro_runner(path: str) -> tuple[str, int] | None:
    """The command that runs a repro test file and the exit code that means 'it FAILED'.
    ``None`` = no runner known for this file type → the repro cannot run → unconfirmed."""
    import shlex  # noqa: PLC0415

    quoted = shlex.quote(path)
    if path.endswith(".py"):
        return (
            f"{REPRO_RUN_PREFIX}python -m pytest -q -x -p no:cacheprovider {quoted}",
            _PYTEST_FAILED,
        )
    if re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", path):
        return f"npx --no-install vitest run {quoted}", _JS_FAILED
    return None


#: The repo's gates run through ``uv run`` (project venv, dependencies installed); a repro runs
#: the same way. A bare ``python`` would not see the project's dependencies (``structlog`` …).
REPRO_RUN_PREFIX = "uv run --all-extras "

#: pytest exit codes that mean 'the test did not run' (collection / import error, interrupted,
#: nothing collected) — as opposed to 1 = 'a test ran and failed'.
_PYTEST_NOT_RUN = (2, 3, 4, 5)
_NOT_RUN_MARKERS = ("ModuleNotFoundError", "command not found")


def repro_command(command: str) -> str:
    """A command repro as the harness runs it: under ``uv run`` like the gates, so ``python``
    and every project import resolve against the project's environment."""
    import shlex  # noqa: PLC0415

    return f"{REPRO_RUN_PREFIX}sh -c {shlex.quote(command)}"


#: Spellings of 'the thing is missing' a repro may legitimately EXPECT to see (a change that
#: forgot a dependency makes ``No module named`` the very proof of the defect).
_EXPECTED_ERROR_MARKERS = (
    "No module named",
    "ModuleNotFoundError",
    "ImportError",
    "command not found",
    "not found",
)


def _expects_the_error(expect: dict[str, Any]) -> bool:
    """The repro itself asks for the shell/import error: in ``contains`` or as exit 126/127."""
    wanted = str(expect.get("contains", "")).lower()
    if any(m.lower() in wanted for m in _EXPECTED_ERROR_MARKERS):
        return True
    code = expect.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code in (126, 127)


def command_did_not_run(
    expect: dict[str, Any], *, exit_code: int | None, output: str, timed_out: bool
) -> bool:
    """The command died before showing anything: shell 'not found' (126/127) or a missing
    module. A claim that itself expects such an error in its output is not 'not run'."""
    if timed_out or exit_code is None:
        return False
    if _expects_the_error(expect):
        return False
    if exit_code in (126, 127):
        return True
    return any(m in output for m in _NOT_RUN_MARKERS)


#: Output saying the test RUNNER itself is missing (no pytest in the venv, no vitest, shell
#: 'not found'). pytest's own exit 1 = 'a test failed' must not hide this.
#: Only the line shapes the interpreter / uv / shell really print are accepted (whole line), so
#: an assert message that merely QUOTES "No module named pytest" is not mistaken for it.
_SHELL = r"(?:(?:sh|bash|zsh|dash)(?:: line \d+)?: )?"
_RUNNER_MISSING = re.compile(
    r"^[^\s:]*python[\d.]*: No module named '?(?:_?pytest|vitest)'?[ \t]*$"
    r"|^error: Failed to spawn: .?(?:pytest|vitest)\b.*$"
    rf"|^{_SHELL}command not found: (?:pytest|vitest|uv|npx)[ \t]*$"
    rf"|^{_SHELL}(?:pytest|vitest|uv|npx): (?:command )?not found[ \t]*$",
    re.MULTILINE,
)

#: Traces of pytest having really run (collection / result summary lines).
_RUNNER_RAN = re.compile(
    r"^=+ test session starts =+"
    r"|^collected \d+ items?\b"
    r"|^=+ .*\b(?:passed|failed|errors?)\b.* in [\d.]+s.*=+\s*$"
    r"|^\d+ (?:passed|failed|errors?)\b",
    re.MULTILINE,
)


def runner_is_missing(output: str) -> bool:
    """The output shows the test runner could not even start (and pytest never ran)."""
    text = output or ""
    if _RUNNER_RAN.search(text):
        return False
    return bool(_RUNNER_MISSING.search(text))


def repro_test_did_not_run(*, exit_code: int | None, timed_out: bool, output: str = "") -> bool:
    """A repro test that never ran: a collection/import error (or nothing collected), or the
    runner itself missing — whatever the exit code (a missing pytest exits 1, like a failure)."""
    if timed_out:
        return False
    return exit_code in _PYTEST_NOT_RUN or runner_is_missing(output)


INTENT_SYSTEM_PROMPT = """\
You judge ONE question. A verifier claims the change broke something and attached a repro that \
holds only AFTER the change. The change is what the REQUEST asked for. Does the behaviour the \
repro expects as 'correct' CONFLICT with a behaviour the REQUEST (or its requirement items) \
told the agent to change? If the repro expects the OLD behaviour the request asked to replace, \
that is a conflict (the new behaviour is intended). If the request does not touch that \
behaviour, it is not a conflict.
Reply with ONE JSON object only: {"conflict": true|false, "why": "one sentence"}."""


def parse_intent_verdict(raw: str) -> tuple[bool, str] | None:
    """(conflict, why) from the judge's reply; ``None`` = not the agreed JSON (→ conservative)."""
    data = _load_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("conflict"), bool):
        return None
    return data["conflict"], _text(data.get("why"), 300)


def clip(text: str, limit: int = OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit}자 생략]"


def judge_command(
    expect: dict[str, Any], *, exit_code: int | None, output: str, timed_out: bool
) -> bool:
    """Does the command's result match what the finding said it would show? Deterministic;
    every stated expectation must hold, a timeout never confirms."""
    if timed_out or exit_code is None:
        return False
    want_code = expect.get("exit_code")
    if want_code == "nonzero" and exit_code == 0:
        return False
    if isinstance(want_code, int) and exit_code != want_code:
        return False
    if "contains" in expect and expect["contains"] not in output:
        return False
    return not ("not_contains" in expect and expect["not_contains"] in output)


def judge_test(*, exit_code: int | None, failed_code: int, timed_out: bool) -> bool:
    """A repro test confirms the finding only by FAILING as a test (not by erroring out)."""
    return not timed_out and exit_code == failed_code


SYSTEM_PROMPT = """\
You are an adversarial verifier. A separate agent produced the change below and every check \
passed. Your job is to BREAK it. You may read files, grep, run commands, and write test files \
ONLY into the scratch directory {scratch}. You cannot edit anything else.

Rule: a claim counts only if you attach a repro the harness can run itself. The harness runs it \
on the changed tree: a repro test that FAILS, or a command whose result matches `expect`, makes \
the claim "confirmed" ONLY if it does not hold before the change (the harness also runs it on \
the tree BEFORE the change, the baseline). A repro that gives the same result before the change \
proves nothing about the change; a grep that merely finds some text is such a repro. Anything \
else is discarded as "unconfirmed". No repro, no finding.
A repro that holds only after the change shows the change CHANGED a behaviour, not that the new \
behaviour is WRONG: a behaviour the REQUEST asked to change is intended. So every finding must \
name `expected_from`, where the behaviour your repro asserts as correct comes from: \
`request` (a verbatim `quote` from the REQUEST or a requirement item, with its `req_id`; the \
`quote` must be literal text of at least 20 characters and 3 words after whitespace \
normalisation, a shorter one is rejected), \
`existing_test` (`test` = `path::name` of a repo test that passed before the change and fails \
after it, or is your repro itself), or `unchanged_contract` (`location` = `file:line` of the \
code BEFORE the change, for behaviour the request did not touch). The harness checks the quote \
and the test mechanically, and a judge asks whether your expectation conflicts with what the \
request asked for. No basis, a false quote, or a conflict with the request = unconfirmed.
Environment: repros run through `uv run` (the same way the repo's gates run), from the \
repository root, so `python` and the project's dependencies are available. A repro that dies \
with an import / collection error is "unconfirmed: the repro did not run".

{protocol}

Surface for this change: {surface}.
{checklist}

Final answer: ONLY this JSON, at most {limit} findings (an empty list is a fine answer):
{{"findings": [{{"severity": "high|medium|low", "claim": "...", "file": "path", "line": 1,
 "repro": {{"kind": "test", "path": "{scratch}/test_x.py"}}
 | {{"kind": "command", "command": "...",
     "expect": {{"exit_code": 0|"nonzero", "contains": "...", "not_contains": "..."}}}},
 "expected_from": {{"kind": "request", "quote": "verbatim text, 20+ chars and 3+ words after whitespace normalisation", "req_id": "R1"}}
   | {{"kind": "existing_test", "test": "tests/x/test_y.py::test_z"}}
   | {{"kind": "unchanged_contract", "location": "backend/x.py:12"}}}}]}}
For a test repro, write it with write_test first; it must FAIL because of the defect. For a \
command repro, `expect` describes the output that PROVES the defect.
"""

CODE_CHECKLIST = """\
Check: (1) is each new function actually called from a production entry point, not only tests? \
(2) does it commit or roll back a session/transaction it does not own? (3) concurrent runs \
(races)? (4) the boundary inputs the request names; (5) regressions in existing behaviour the \
diff touches."""

ARTIFACT_CHECKLIST = """\
Check: (1) every number and claim — re-run the source command or query and compare; confirm with \
`not_contains` (the report's figure absent from the source's output) or `exit_code`. (2) every \
path, function, command and link named in the document actually exists (`test -e`, `git grep`, \
`--help`, a HEAD request). (3) stated constraints (length, format, banned words) with a \
deterministic command. Tone and anything a command cannot decide: do not file it."""


def checklist(surface: str) -> str:
    return ARTIFACT_CHECKLIST if surface == "artifact" else CODE_CHECKLIST


TOOL_NAMES = ("file_read", "grep", "shell_exec", "write_test")

PROTOCOL = """\
You have no native tools. Every reply is ONE JSON object and nothing else \u2014 either a tool request
{"tool": "file_read", "args": {"path": "..."}}      read a file in the workspace
{"tool": "grep", "args": {"pattern": "..."}}        git grep -n for a pattern
{"tool": "shell_exec", "args": {"command": "..."}}  run a shell command in the workspace
{"tool": "write_test", "args": {"path": "...", "content": "..."}}  write a TEST file (scratch dir only)
or the final answer (below). The harness runs a tool request and replies with its output."""
