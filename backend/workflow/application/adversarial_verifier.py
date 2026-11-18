"""The adversarial verifier — a separate agent session that tries to break a passed round.

Report-only (stage 1): this never changes the round's verdict. The model proposes findings, each
with a repro; the HARNESS runs every repro and decides confirmed / unconfirmed from the exit code
and output (``domain.adversarial``). The model never judges.

Isolation: the session AND the repros run in a throwaway COPY of the verified state (committed and
uncommitted changes, with its own fresh ``.git``), never in the real working tree. The real tree
is fingerprinted (HEAD + ``git status`` + a hash of the diff and of untracked files) before and
after; a difference discards the result and records a harness error.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from backend.workflow.domain.adversarial import (
    MAX_FINDINGS,
    SCRATCH_ROOT,
    SYSTEM_PROMPT,
    TOOLS,
    Finding,
    checklist,
    clip,
    judge_command,
    judge_test,
    parse_findings,
    repro_runner,
    write_refusal,
)
from backend.workflow.infrastructure.sandbox import SandboxResult, SandboxSession

logger = structlog.get_logger(__name__)

#: ONE time budget for the whole stage — the copy, the session, every repro and the cleanup.
#: Same ceiling as the existing review's single call; turns/tokens bound the tool loop. The last
#: ``_RESERVE_S`` is held back so the cleanup and the real-tree check always get to run.
_BUDGET_S = 180.0
_RESERVE_S = 30.0
_MAX_TURNS = 12
_TOKEN_CAP = 120_000
_EXEC_TIMEOUT_S = 60.0
_REPRO_TIMEOUT_S = 120.0
_TOOL_OUTPUT_CHARS = 4000
_CHECKS_SHOWN = 30
_DIFF_CHARS = 30_000
_COPY_ROOT = "/tmp/bsvibe-adversarial"  # noqa: S108 — outside the workspace on purpose
_UNTRACKED_HASH = (
    "git ls-files -o --exclude-standard -z | xargs -0 -r sha256sum 2>/dev/null | sha256sum"
)

Llm = Any  # JudgeLlm: ``complete(messages=..., tools=...)`` → a turn with usage fields


class _Budget:
    """The stage's single clock. Everything that takes time asks it how much is left."""

    def __init__(self, total_s: float = _BUDGET_S, reserve_s: float = _RESERVE_S) -> None:
        self._start = time.monotonic()
        self._total = total_s
        self._reserve = reserve_s

    def remaining(self) -> float:
        """Seconds left for the work (session + repros); the reserve is not counted."""
        return self._total - self._reserve - (time.monotonic() - self._start)

    def cleanup_s(self) -> float:
        """What the cleanup may take: whatever is left, but never less than the reserve's half."""
        left = self._total - (time.monotonic() - self._start)
        return max(left, self._reserve / 2)


class _Copy:
    """The verifier's view of the workspace: a copy at ``root``, driven through the real box.

    Looks like a ``SandboxSession`` whose workspace is the copy, so every command — the model's
    shell, its greps, the repros — lands there. Nothing it does reaches the real tree through
    this object (an absolute path in a shell command still could; the fingerprint catches that).
    """

    def __init__(self, box: SandboxSession, root: str) -> None:
        self._box = box
        self.root = root

    @property
    def workspace_mount(self) -> str:
        return self.root

    async def exec(self, command: str, *, timeout_s: float) -> SandboxResult:
        # The verifier's tools ARE a shell (that is the point); the copy is the boundary.
        return await self._box.exec(  # noqa: S604
            f"cd {shlex.quote(self.root)} && {command}", timeout_s=timeout_s, shell=True
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        res = await self.exec(
            f"head -c {int(max_bytes)} -- {shlex.quote(rel_path)}", timeout_s=_EXEC_TIMEOUT_S
        )
        if res.exit_code != 0:
            raise FileNotFoundError(rel_path)
        return (res.stdout or "").encode()

    async def write_file(self, rel_path: str, content: bytes) -> None:
        quoted = shlex.quote(rel_path)
        body = base64.b64encode(content).decode()
        res = await self.exec(
            f'mkdir -p -- "$(dirname {quoted})" && echo {body} | base64 -d > {quoted}',
            timeout_s=_EXEC_TIMEOUT_S,
        )
        if res.exit_code != 0:
            raise OSError(f"write failed: {rel_path}")


async def run_adversarial_round(
    *,
    llm: Llm,
    run: Any,
    box: SandboxSession,
    baseline: str,
    surface: str,
    release: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """The ``result["adversarial"]`` blob. Never raises: a hiccup is ``status: unavailable``."""
    budget = _Budget()
    scratch = f"{SCRATCH_ROOT}/{str(run.id)[:12]}"
    checks: list[str] = []
    copy = _Copy(box, f"{_COPY_ROOT}-{str(run.id)[:12]}-{uuid.uuid4().hex[:8]}")
    blob: dict[str, Any]
    try:
        before = await _fingerprint(box)
        try:
            await release()
            diff = await _working_tree_diff(box, baseline)
            await _make_copy(box, copy)
            findings = await _bounded(
                _session(llm, run, copy, diff, surface, scratch, checks, budget), budget
            )
            if findings is None:
                blob = _blob(
                    "unavailable",
                    surface,
                    [],
                    checks,
                    "검증자가 끝내지 못했거나 출력을 해석하지 못했다",
                )
            else:
                judged = [await _judge(box, copy, scratch, f, budget) for f in findings]
                blob = _blob("done", surface, judged, checks)
        finally:
            await _cleanup(box, copy, budget)
        if await _fingerprint(box) != before:
            logger.error("adversarial_real_tree_changed", run_id=str(run.id))
            return _blob(
                "unavailable",
                surface,
                [],
                checks,
                "harness_error: 검증 단계 중 실제 작업 트리가 바뀌어 결과를 버렸다",
            )
        return blob
    except Exception as exc:  # noqa: BLE001 — report-only: must never change the verdict
        logger.warning("adversarial_unavailable", run_id=str(run.id), error=repr(exc)[:200])
        return _blob("unavailable", surface, [], checks, f"{type(exc).__name__}: {exc}"[:300])


async def _bounded(coro: Awaitable[Any], budget: _Budget) -> Any:
    """Run ``coro`` within what the budget has left; out of time → ``None`` (not an error)."""
    left = budget.remaining()
    if left <= 0:
        close = getattr(coro, "close", None)
        if close:
            close()
        return None
    try:
        return await asyncio.wait_for(coro, left)
    except TimeoutError:
        return None


def _blob(
    status: str, surface: str, findings: list[dict[str, Any]], checks: list[str], reason: str = ""
) -> dict[str, Any]:
    confirmed = sum(1 for f in findings if f["confirmed"])
    return {
        "status": status,
        "surface": surface,
        "findings": findings,
        "confirmed": confirmed,
        "unconfirmed": len(findings) - confirmed,
        "checks": checks[:_CHECKS_SHOWN],
        **({"reason": reason} if reason else {}),
    }


async def _session(
    llm: Llm,
    run: Any,
    copy: _Copy,
    diff: str,
    surface: str,
    scratch: str,
    checks: list[str],
    budget: _Budget,
) -> list[Finding] | None:
    payload = run.payload if isinstance(run.payload, dict) else {}
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                scratch=scratch,
                surface=surface,
                checklist=checklist(surface),
                limit=MAX_FINDINGS,
            ),
        },
        {
            "role": "user",
            "content": (
                f"REQUEST:\n{payload.get('intent_text') or payload.get('text') or ''}\n\n"
                f"CHANGE (git diff, may be cut):\n{diff[:_DIFF_CHARS]}"
            ),
        },
    ]
    spent = 0
    for _ in range(_MAX_TURNS):
        turn = await _turn(llm, run, messages, TOOLS if spent < _TOKEN_CAP else None)
        spent += int(getattr(turn, "usage_prompt_tokens", 0) or 0) + int(
            getattr(turn, "usage_completion_tokens", 0) or 0
        )
        calls = tuple(getattr(turn, "tool_calls", ()) or ())
        if not calls:
            return parse_findings(str(getattr(turn, "content", "") or ""))
        messages.append(
            {
                "role": "assistant",
                "content": getattr(turn, "content", None) or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ],
            }
        )
        for call in calls:
            output = await _dispatch(copy, scratch, call.name, call.arguments, checks, budget)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
    # Out of turns: ask for the answer without tools.
    messages.append({"role": "user", "content": "Stop investigating. Give the final JSON now."})
    turn = await _turn(llm, run, messages, None)
    return parse_findings(str(getattr(turn, "content", "") or ""))


async def _turn(llm: Llm, run: Any, messages: list[dict[str, Any]], tools: Any) -> Any:
    turn = await llm.complete(messages=list(messages), tools=tools)
    # Metered like every other verify call (#930).
    run.usage_prompt_tokens += int(getattr(turn, "usage_prompt_tokens", 0) or 0)
    run.usage_completion_tokens += int(getattr(turn, "usage_completion_tokens", 0) or 0)
    return turn


async def _dispatch(  # noqa: PLR0911 — one return per tool, flat on purpose
    copy: _Copy,
    scratch: str,
    name: str,
    args: dict[str, Any],
    checks: list[str],
    budget: _Budget,
) -> str:
    """One verifier tool call, always against the copy. Writes go through the guard."""
    try:
        if name == "file_read":
            path = str(args.get("path", ""))
            checks.append(f"file_read {path}"[:200])
            return clip(
                (await copy.read_file(path, _TOOL_OUTPUT_CHARS)).decode(errors="replace"),
                _TOOL_OUTPUT_CHARS,
            )
        if name == "grep":
            pattern = str(args.get("pattern", ""))
            checks.append(f"grep {pattern}"[:200])
            cmd = f"git -C {shlex.quote(copy.root)} grep -n -e {shlex.quote(pattern)}"
            res = await copy.exec(cmd, timeout_s=_step_timeout(budget, _EXEC_TIMEOUT_S))
            return clip(res.stdout or "", _TOOL_OUTPUT_CHARS)
        if name == "shell_exec":
            command = str(args.get("command", ""))
            checks.append(f"$ {command}"[:200])
            result = await copy.exec(command, timeout_s=_step_timeout(budget, _EXEC_TIMEOUT_S))
            body = f"exit={result.exit_code}{' (timeout)' if result.timed_out else ''}\n"
            return clip(body + (result.stdout or "") + (result.stderr or ""), _TOOL_OUTPUT_CHARS)
        if name == "write_test":
            path = str(args.get("path", ""))
            refusal = write_refusal(path, scratch)
            if refusal is not None:
                checks.append(f"write_test {path} (거절)"[:200])
                return f"REFUSED: {refusal}"
            checks.append(f"write_test {path}"[:200])
            await copy.write_file(path, str(args.get("content", "")).encode())
            return "written"
    except Exception as exc:  # noqa: BLE001 — a tool error is the model's to read, not ours to raise
        return f"ERROR: {type(exc).__name__}: {exc}"[:500]
    return f"ERROR: unknown tool {name}"


def _step_timeout(budget: _Budget, cap: float) -> float:
    """A single command's timeout: its own cap, never more than the budget has left."""
    return max(min(cap, budget.remaining()), 1.0)


def _unconfirmed(finding: Finding, reason: str, output: str = "") -> dict[str, Any]:
    repro = finding.repro
    return {
        "severity": finding.severity,
        "claim": finding.claim,
        "file": finding.file,
        "line": finding.line,
        "repro": {
            "kind": repro.kind,
            **({"path": repro.path} if repro.path else {}),
            **({"command": repro.command, "expect": repro.expect} if repro.command else {}),
        },
        "confirmed": False,
        "reason": reason,
        "output": clip(output),
    }


async def _judge(
    box: SandboxSession,
    worked_in: _Copy,
    scratch: str,
    finding: Finding,
    budget: _Budget,
) -> dict[str, Any]:
    """Run the repro in a FRESH copy of the verified state; the verdict is the exit code / output.

    The verifier's own copy is never the place a repro runs: whatever it edited there (a broken
    production function, say) must not reach the verdict. The fresh copy gets only the finding's
    repro test file, taken from the scratch directory of the verifier's copy."""
    repro = finding.repro
    if budget.remaining() <= 0:
        return _unconfirmed(finding, "시간 예산이 소진되어 실행하지 못했다")
    confirmed, reason, output = False, "", ""
    timeout = _step_timeout(budget, _REPRO_TIMEOUT_S)
    fresh = _Copy(box, f"{worked_in.root}-repro-{uuid.uuid4().hex[:6]}")
    try:
        await _make_copy(box, fresh)
        if repro.kind == "test":
            path = repro.path or ""
            runner = repro_runner(path)
            if write_refusal(path, scratch) is not None:
                reason = "재현 테스트가 임시 디렉터리 밖에 있어 실행하지 않았다"
            elif runner is None:
                reason = "이 파일 형식의 테스트 실행기를 알 수 없다"
            else:
                command, failed_code = runner
                await _carry_repro_file(box, worked_in, fresh, path)
                res = await fresh.exec(command, timeout_s=timeout)
                output = (res.stdout or "") + (res.stderr or "")
                confirmed = judge_test(
                    exit_code=res.exit_code, failed_code=failed_code, timed_out=res.timed_out
                )
                reason = (
                    "재현 테스트가 변경에서 실패했다"
                    if confirmed
                    else "시간 초과"
                    if res.timed_out
                    else "재현 테스트가 통과했거나 실행되지 않았다"
                )
        else:
            res = await fresh.exec(repro.command or "", timeout_s=timeout)
            output = (res.stdout or "") + (res.stderr or "")
            confirmed = judge_command(
                repro.expect, exit_code=res.exit_code, output=output, timed_out=res.timed_out
            )
            reason = (
                "재현 명령의 결과가 기대와 일치했다"
                if confirmed
                else "시간 초과"
                if res.timed_out
                else "재현 명령의 결과가 기대와 달랐다"
            )
    except Exception as exc:  # noqa: BLE001 — a repro that cannot run is unconfirmed
        confirmed, reason = False, f"재현을 실행할 수 없다: {type(exc).__name__}"
    finally:
        await _cleanup(box, fresh, budget)
    return {**_unconfirmed(finding, reason, output), "confirmed": confirmed}


async def _carry_repro_file(box: SandboxSession, src: _Copy, dst: _Copy, rel_path: str) -> None:
    """Copy ONE file (the repro test, inside the scratch dir) from the verifier's copy into the
    fresh one. Nothing else the verifier did travels."""
    s, d = shlex.quote(f"{src.root}/{rel_path}"), shlex.quote(f"{dst.root}/{rel_path}")
    await _sh(box, f'mkdir -p -- "$(dirname {d})" && cp -- {s} {d}')


async def _working_tree_diff(box: SandboxSession, baseline: str) -> str:
    """The change as the working tree stands against the baseline — uncommitted edits included
    (``git diff baseline HEAD`` would miss them) — plus the names of untracked files."""
    mount = shlex.quote(box.workspace_mount)
    return await _sh(
        box,
        f"cd {mount} && git diff {shlex.quote(baseline)} && "
        "echo '--- untracked files ---' && git ls-files -o --exclude-standard",
    )


async def _real(box: SandboxSession, command: str, timeout_s: float = _EXEC_TIMEOUT_S) -> Any:
    return await box.exec(command, timeout_s=timeout_s, shell=True)  # noqa: S604


async def _sh(box: SandboxSession, command: str) -> str:
    res = await _real(box, command)
    if res.timed_out or res.exit_code != 0:
        raise RuntimeError(f"command failed (exit {res.exit_code}): {command[:80]}")
    return res.stdout or ""


async def _fingerprint(box: SandboxSession) -> str:
    """HEAD + ``git status --porcelain`` + a hash of ``git diff HEAD`` and of the untracked files.

    Any of: a moved HEAD, an edit to an already-modified file, a reverted edit, a new or changed
    untracked file shows up as a different string."""
    mount = shlex.quote(box.workspace_mount)
    return await _sh(
        box,
        f"cd {mount} && git rev-parse HEAD && git status --porcelain && "
        f"git diff HEAD | sha256sum && {_UNTRACKED_HASH}",
    )


async def _make_copy(box: SandboxSession, copy: _Copy) -> None:
    """Copy the verified state — uncommitted changes included, ``.git`` excluded — to a fresh
    directory and give it its OWN repository, so the verifier's git commands cannot touch the
    real one (a worktree's ``.git`` file would point back at the shared gitdir)."""
    mount, root = shlex.quote(box.workspace_mount), shlex.quote(copy.root)
    await _sh(
        box,
        f"mkdir -p {root} && cd {mount} && tar --exclude=./.git -cf - . | tar -xf - -C {root} && "
        f"cd {root} && git init -q && git add -A -f && "
        "git -c user.name=harness -c user.email=harness@localhost commit -q --no-verify "
        "--allow-empty -m verified-state",
    )


async def _cleanup(box: SandboxSession, copy: _Copy, budget: _Budget) -> None:
    """Delete the copy — and with it the scratch test directory. Never raises."""
    try:
        await _real(box, f"rm -rf -- {shlex.quote(copy.root)}", budget.cleanup_s())
    except Exception as exc:  # noqa: BLE001 — cleanup must not break the round
        logger.warning("adversarial_cleanup_failed", error=repr(exc)[:200])
