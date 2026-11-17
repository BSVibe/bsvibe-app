"""The adversarial verifier — a separate agent session that tries to break a passed round.

Report-only (stage 1): this never changes the round's verdict. The model proposes findings, each
with a repro; the HARNESS runs every repro on the changed tree and decides confirmed /
unconfirmed from the exit code and output (``domain.adversarial``). The model never judges.
"""

from __future__ import annotations

import asyncio
import json
import shlex
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
from backend.workflow.infrastructure.sandbox import SandboxSession

logger = structlog.get_logger(__name__)

#: Same ceiling as the existing review's single call; turns/tokens bound the tool loop.
_TIMEOUT_S = 180.0
_MAX_TURNS = 12
_TOKEN_CAP = 120_000
_EXEC_TIMEOUT_S = 60.0
_REPRO_TIMEOUT_S = 120.0
_TOOL_OUTPUT_CHARS = 4000
_CHECKS_SHOWN = 30
_DIFF_CHARS = 30_000

Llm = Any  # JudgeLlm: ``complete(messages=..., tools=...)`` → a turn with usage fields


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
    scratch = f"{SCRATCH_ROOT}/{str(run.id)[:12]}"
    checks: list[str] = []
    try:
        before = await _status(box)
        try:
            await release()
            findings = await asyncio.wait_for(
                _session(llm, run, box, baseline, surface, scratch, checks), _TIMEOUT_S
            )
            if findings is None:
                return _blob("unavailable", surface, [], checks, "검증자 출력을 해석하지 못했다")
            judged = [await _judge(box, scratch, f) for f in findings]
            return _blob("done", surface, judged, checks)
        finally:
            await _cleanup(box, scratch, before)
    except Exception as exc:  # noqa: BLE001 — report-only: must never change the verdict
        logger.warning("adversarial_unavailable", run_id=str(run.id), error=repr(exc)[:200])
        return _blob("unavailable", surface, [], checks, f"{type(exc).__name__}: {exc}"[:300])


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
    box: SandboxSession,
    baseline: str,
    surface: str,
    scratch: str,
    checks: list[str],
) -> list[Finding] | None:
    diff = await _sh(
        box, f"git -C {shlex.quote(box.workspace_mount)} diff {shlex.quote(baseline)} HEAD"
    )
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
            output = await _dispatch(box, scratch, call.name, call.arguments, checks)
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
    box: SandboxSession, scratch: str, name: str, args: dict[str, Any], checks: list[str]
) -> str:
    """One verifier tool call. Read/exec reuse the sandbox; writes go through the guard."""
    try:
        if name == "file_read":
            path = str(args.get("path", ""))
            checks.append(f"file_read {path}"[:200])
            return clip(
                (await box.read_file(path, _TOOL_OUTPUT_CHARS)).decode(errors="replace"),
                _TOOL_OUTPUT_CHARS,
            )
        if name == "grep":
            pattern = str(args.get("pattern", ""))
            checks.append(f"grep {pattern}"[:200])
            cmd = f"git -C {shlex.quote(box.workspace_mount)} grep -n -e {shlex.quote(pattern)}"
            return clip(await _sh(box, cmd, ok=(0, 1)), _TOOL_OUTPUT_CHARS)
        if name == "shell_exec":
            command = str(args.get("command", ""))
            checks.append(f"$ {command}"[:200])
            result = await _exec(box, command, _EXEC_TIMEOUT_S)
            body = f"exit={result.exit_code}{' (timeout)' if result.timed_out else ''}\n"
            return clip(body + (result.stdout or "") + (result.stderr or ""), _TOOL_OUTPUT_CHARS)
        if name == "write_test":
            path = str(args.get("path", ""))
            refusal = write_refusal(path, scratch)
            if refusal is not None:
                checks.append(f"write_test {path} (거절)"[:200])
                return f"REFUSED: {refusal}"
            checks.append(f"write_test {path}"[:200])
            await box.write_file(path, str(args.get("content", "")).encode())
            return "written"
    except Exception as exc:  # noqa: BLE001 — a tool error is the model's to read, not ours to raise
        return f"ERROR: {type(exc).__name__}: {exc}"[:500]
    return f"ERROR: unknown tool {name}"


async def _judge(box: SandboxSession, scratch: str, finding: Finding) -> dict[str, Any]:
    """Run the repro on the changed tree; the verdict is the exit code / output, nothing else."""
    repro = finding.repro
    confirmed, reason, output = False, "", ""
    try:
        if repro.kind == "test":
            path = repro.path or ""
            runner = repro_runner(path)
            if write_refusal(path, scratch) is not None:
                reason = "재현 테스트가 임시 디렉터리 밖에 있어 실행하지 않았다"
            elif runner is None:
                reason = "이 파일 형식의 테스트 실행기를 알 수 없다"
            else:
                command, failed_code = runner
                res = await _exec(box, command, _REPRO_TIMEOUT_S)
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
            res = await _exec(box, repro.command or "", _REPRO_TIMEOUT_S)
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
        reason = f"재현을 실행할 수 없다: {type(exc).__name__}"
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
        "confirmed": confirmed,
        "reason": reason,
        "output": clip(output),
    }


async def _exec(box: SandboxSession, command: str, timeout_s: float) -> Any:
    # The verifier's tools ARE a shell (that is the point); the sandbox is the boundary.
    return await box.exec(command, timeout_s=timeout_s, shell=True)  # noqa: S604


async def _sh(box: SandboxSession, command: str, ok: tuple[int, ...] = (0,)) -> str:
    res = await _exec(box, command, _EXEC_TIMEOUT_S)
    if res.timed_out or res.exit_code not in ok:
        raise RuntimeError(f"command failed (exit {res.exit_code}): {command[:80]}")
    return res.stdout or ""


async def _status(box: SandboxSession) -> set[str]:
    out = await _sh(box, f"git -C {shlex.quote(box.workspace_mount)} status --porcelain")
    return {ln for ln in out.splitlines() if SCRATCH_ROOT not in ln}


async def _cleanup(box: SandboxSession, scratch: str, before: set[str]) -> None:
    """Remove the scratch dir and undo any write outside it (a shell command can write too).

    The verified tree must come out exactly as it went in."""
    try:
        mount = shlex.quote(box.workspace_mount)
        await _exec(
            box,
            f"cd {mount} && rm -rf {shlex.quote(scratch)} && rmdir {SCRATCH_ROOT} 2>/dev/null; true",
            _EXEC_TIMEOUT_S,
        )
        for line in sorted(await _status(box) - before):
            status, path = line[:2], line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            quoted = shlex.quote(path)
            fix = (
                f"rm -rf -- {quoted}" if status == "??" else f"git -C {mount} checkout -- {quoted}"
            )
            await _exec(box, f"cd {mount} && {fix}", _EXEC_TIMEOUT_S)
            logger.warning("adversarial_stray_write_undone", path=path[:200])
    except Exception as exc:  # noqa: BLE001 — cleanup must not break the round
        logger.warning("adversarial_cleanup_failed", error=repr(exc)[:200])
