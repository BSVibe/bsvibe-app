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


@dataclass(frozen=True)
class Finding:
    severity: str
    claim: str
    file: str | None
    line: int | None
    repro: Repro


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
REPRO_RUN_PREFIX = "uv run --frozen --all-extras "

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
_RUNNER_MISSING = re.compile(
    r"No module named '?(?:_?pytest|vitest)\b"
    r"|command not found: (?:pytest|vitest|uv|npx)\b"
    r"|(?:pytest|vitest|uv|npx): (?:command )?not found"
    r"|Failed to spawn: .?(?:pytest|vitest)"
)


def runner_is_missing(output: str) -> bool:
    """The output shows the test runner could not even start."""
    return bool(_RUNNER_MISSING.search(output or ""))


def repro_test_did_not_run(*, exit_code: int | None, timed_out: bool, output: str = "") -> bool:
    """A repro test that never ran: a collection/import error (or nothing collected), or the
    runner itself missing — whatever the exit code (a missing pytest exits 1, like a failure)."""
    if timed_out:
        return False
    return exit_code in _PYTEST_NOT_RUN or runner_is_missing(output)


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
     "expect": {{"exit_code": 0|"nonzero", "contains": "...", "not_contains": "..."}}}}}}]}}
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
