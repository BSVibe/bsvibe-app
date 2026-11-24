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
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from backend.workflow.domain.adversarial import (
    MAX_FINDINGS,
    PROTOCOL,
    SCRATCH_ROOT,
    SYSTEM_PROMPT,
    Finding,
    ToolRequest,
    checklist,
    clip,
    command_did_not_run,
    judge_command,
    judge_test,
    parse_reply,
    repro_command,
    repro_runner,
    repro_test_did_not_run,
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


class _TokenCapExceeded(Exception):
    """The verifier spent its token cap: the loop ends at once, nothing it still wanted counts."""


class _BaselineUnavailable(Exception):
    """The tree before the change could not be built: the repro is not confirmed."""


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
    tag = f"{str(run.id)[:12]}-{uuid.uuid4().hex[:8]}"
    copy = _Copy(box, f"{_COPY_ROOT}-{tag}")
    snapshot = f"{_COPY_ROOT}-snap-{tag}.tar"
    guarded = False
    try:
        before = await _fingerprint(box)
        await _snapshot(box, snapshot)
        guarded = True
        try:
            await release()
            diff = await _working_tree_diff(box, baseline)
            await _make_copy(box, copy)
            probes = [0]
            findings = await _bounded(
                _session(llm, run, copy, diff, surface, scratch, checks, budget, probes), budget
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
                judged = [
                    await _judge(box, copy, scratch, f, budget, baseline=baseline) for f in findings
                ]
                blob = _blob("done" if probes[0] else "no_probe", surface, judged, checks)
                blob["probes"] = probes[0]
        except _TokenCapExceeded:
            blob = _blob("unavailable", surface, [], checks, "토큰 상한 초과로 검증자를 멈췄다")
        except Exception as exc:  # noqa: BLE001 — report-only: must never change the verdict
            logger.warning("adversarial_unavailable", run_id=str(run.id), error=repr(exc)[:600])
            blob = _blob("unavailable", surface, [], checks, f"{type(exc).__name__}: {exc}"[:450])
        finally:
            await _cleanup(box, copy, budget)
        return await _guard_real_tree(box, before, snapshot, run, surface, checks, blob)
    except Exception as exc:  # noqa: BLE001 — report-only: must never change the verdict
        logger.warning("adversarial_unavailable", run_id=str(run.id), error=repr(exc)[:600])
        why = f"{type(exc).__name__}: {exc}"[:450]
        if guarded:  # the stage had started: the real tree must still be proven clean
            return _unrestored(surface, checks, why)
        return _blob("unavailable", surface, [], checks, why)
    finally:
        try:
            await _real(box, f"rm -f -- {shlex.quote(snapshot)}", _EXEC_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — the snapshot is outside the workspace
            logger.warning("adversarial_snapshot_cleanup_failed", error=repr(exc)[:200])


def _unrestored(surface: str, checks: list[str], why: str) -> dict[str, Any]:
    """The real tree could not be proven identical to its pre-stage state: fail the round."""
    reason = f"harness_error: 작업 트리를 복원하지 못했다 ({why})"
    return {**_blob("unavailable", surface, [], checks, reason), "tree_unrestored": True}


async def _guard_real_tree(
    box: SandboxSession,
    before: str,
    snapshot: str,
    run: Any,
    surface: str,
    checks: list[str],
    blob: dict[str, Any],
) -> dict[str, Any]:
    """Guarantee the real tree equals its pre-stage state; detecting is not enough.

    Same fingerprint: the verifier's result stands. Different: the result is discarded, the
    tree is restored from the snapshot and re-fingerprinted; if that does not match either, the
    round fails (``tree_unrestored``) so a polluted tree never reaches the ledger or delivery."""
    if await _fingerprint(box) == before:
        return blob
    logger.error("adversarial_real_tree_changed", run_id=str(run.id))
    try:
        await _restore(box, snapshot)
        restored = await _fingerprint(box) == before
    except Exception as exc:  # noqa: BLE001 — an unverifiable restore is a failed round
        logger.error("adversarial_restore_failed", run_id=str(run.id), error=repr(exc)[:200])
        restored = False
    if not restored:
        return _unrestored(surface, checks, "복원 후 지문이 다르다")
    return _blob(
        "unavailable",
        surface,
        [],
        checks,
        "harness_error: 검증 단계 중 실제 작업 트리가 바뀌어 복원하고 결과를 버렸다",
    )


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


_NO_PROBE_NUDGE = (
    "You gave a final answer without running any tool. Before the final answer, read or run "
    "the change at least once (file_read, grep, shell_exec or write_test) to check it "
    "yourself, then give the final JSON."
)


async def _session(
    llm: Llm,
    run: Any,
    copy: _Copy,
    diff: str,
    surface: str,
    scratch: str,
    checks: list[str],
    budget: _Budget,
    probes: list[int] | None = None,
) -> list[Finding] | None:
    probes = probes if probes is not None else [0]
    payload = run.payload if isinstance(run.payload, dict) else {}
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                scratch=scratch,
                surface=surface,
                checklist=checklist(surface),
                limit=MAX_FINDINGS,
                protocol=PROTOCOL,
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
    nudged = False
    first_answer: list[Finding] | None = None  # kept: a failed nudge must not lose it
    for _ in range(_MAX_TURNS):
        reply, used = await _ask(llm, run, messages)
        spent += used
        if reply is None:
            return first_answer  # malformed twice: the first answer if any, else unavailable
        if isinstance(reply, list):
            if probes[0] == 0 and not nudged and spent < _TOKEN_CAP:
                # Answered without touching the change: send it back ONCE (counts as a turn).
                nudged = True
                first_answer = reply
                messages.append({"role": "user", "content": _NO_PROBE_NUDGE})
                continue
            return reply
        if spent >= _TOKEN_CAP:
            # A real stop: no further tool call runs and no further model call is made.
            raise _TokenCapExceeded
        output = await _dispatch(copy, scratch, reply.name, reply.args, checks, budget, probes)
        messages.append(
            {"role": "user", "content": f"TOOL RESULT ({reply.name}):\n{output}"},
        )
    # Out of turns: ask for the answer.
    messages.append({"role": "user", "content": "Stop investigating. Give the final JSON now."})
    reply, _ = await _ask(llm, run, messages)
    return reply if isinstance(reply, list) else first_answer


async def _ask(
    llm: Llm, run: Any, messages: list[dict[str, Any]]
) -> tuple[ToolRequest | list[Finding] | None, int]:
    """One plain chat turn (no ``tools``: the harness runs the tool loop itself). A reply that is
    not the agreed JSON is asked for again ONCE; still wrong \u2192 ``None``. Appends to ``messages``."""
    spent = 0
    for attempt in range(2):
        turn = await _turn(llm, run, messages, None)
        spent += int(getattr(turn, "usage_prompt_tokens", 0) or 0) + int(
            getattr(turn, "usage_completion_tokens", 0) or 0
        )
        content = str(getattr(turn, "content", "") or "")
        messages.append({"role": "assistant", "content": content})
        parsed = parse_reply(content)
        if parsed is not None:
            return parsed, spent
        if attempt == 0:
            messages.append(
                {
                    "role": "user",
                    "content": "FORMAT ERROR: reply with ONE JSON object only \u2014 "
                    '{"tool": ..., "args": {...}} or {"findings": [...]}.',
                }
            )
    return None, spent


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
    probes: list[int] | None = None,
) -> str:
    """One verifier tool call, always against the copy. Writes go through the guard.

    ``probes[0]`` counts only requests that actually RAN: a refused write, an unknown tool or an
    error does not count."""
    ran = probes if probes is not None else [0]
    try:
        if name == "file_read":
            path = str(args.get("path", ""))
            checks.append(f"file_read {path}"[:200])
            data = await copy.read_file(path, _TOOL_OUTPUT_CHARS)
            ran[0] += 1
            return clip(data.decode(errors="replace"), _TOOL_OUTPUT_CHARS)
        if name == "grep":
            pattern = str(args.get("pattern", ""))
            checks.append(f"grep {pattern}"[:200])
            cmd = f"git -C {shlex.quote(copy.root)} grep -n -e {shlex.quote(pattern)}"
            res = await copy.exec(cmd, timeout_s=_step_timeout(budget, _EXEC_TIMEOUT_S))
            ran[0] += 1
            return clip(res.stdout or "", _TOOL_OUTPUT_CHARS)
        if name == "shell_exec":
            command = str(args.get("command", ""))
            checks.append(f"$ {command}"[:200])
            result = await copy.exec(command, timeout_s=_step_timeout(budget, _EXEC_TIMEOUT_S))
            ran[0] += 1
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
            ran[0] += 1
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
    *,
    baseline: str = "",
) -> dict[str, Any]:
    """Run the repro in a FRESH copy of the verified state; the verdict is the exit code / output.

    The verifier's own copy is never the place a repro runs: whatever it edited there (a broken
    production function, say) must not reach the verdict. The fresh copy gets only the finding's
    repro test file, taken from the scratch directory of the verifier's copy.

    A repro that shows the defect AFTER the change is then run again on a copy of the tree BEFORE
    it (the baseline control): a result that already held before is not the change's doing."""
    repro = finding.repro
    if budget.remaining() <= 0:
        return _unconfirmed(finding, "시간 예산이 소진되어 실행하지 못했다")
    timeout = _step_timeout(budget, _REPRO_TIMEOUT_S)
    fresh = _Copy(box, f"{worked_in.root}-repro-{uuid.uuid4().hex[:6]}")
    verdict: tuple[bool, str, str, dict[str, Any]]
    try:
        await _make_copy(box, fresh)
        if repro.kind == "test":
            verdict = await _judge_test(
                box, worked_in, fresh, scratch, finding, baseline, budget, timeout
            )
        else:
            verdict = await _judge_command(
                box, worked_in, fresh, finding, baseline, budget, timeout
            )
    except _BaselineUnavailable as exc:
        verdict = (False, f"변경 전 대조를 하지 못했다 ({str(exc)[:120]})", "", {})
    except Exception as exc:  # noqa: BLE001 — a repro that cannot run is unconfirmed
        verdict = (False, f"재현을 실행할 수 없다: {type(exc).__name__}", "", {})
    finally:
        await _cleanup(box, fresh, budget)
    confirmed, reason, output, extra = verdict
    return {**_unconfirmed(finding, reason, output), "confirmed": confirmed, **extra}


async def _judge_test(
    box: SandboxSession,
    worked_in: _Copy,
    fresh: _Copy,
    scratch: str,
    finding: Finding,
    baseline: str,
    budget: _Budget,
    step_s: float,
) -> tuple[bool, str, str, dict[str, Any]]:
    """(confirmed, reason, output, extra) for a test repro."""
    path = finding.repro.path or ""
    runner = repro_runner(path)
    if write_refusal(path, scratch) is not None:
        return False, "재현 테스트가 임시 디렉터리 밖에 있어 실행하지 않았다", "", {}
    if runner is None:
        return False, "이 파일 형식의 테스트 실행기를 알 수 없다", "", {}
    command, failed_code = runner
    await _carry_repro_file(box, worked_in, fresh, path)
    res = await fresh.exec(command, timeout_s=step_s)
    output = (res.stdout or "") + (res.stderr or "")
    if res.timed_out:
        return False, "시간 초과", output, {}
    if judge_test(exit_code=res.exit_code, failed_code=failed_code, timed_out=False):
        before = await _before_change(box, worked_in, command, path, baseline, budget)
        confirmed, reason, extra = _control_test(before, failed_code)
        return confirmed, reason, output, extra
    if repro_test_did_not_run(exit_code=res.exit_code, timed_out=False):
        reason = f"재현이 실행되지 않음 (수집·import 오류, exit {res.exit_code})"
        return False, reason, output, {"not_run": True}
    return False, "재현 테스트가 통과했다", output, {}


async def _judge_command(
    box: SandboxSession,
    worked_in: _Copy,
    fresh: _Copy,
    finding: Finding,
    baseline: str,
    budget: _Budget,
    step_s: float,
) -> tuple[bool, str, str, dict[str, Any]]:
    """(confirmed, reason, output, extra) for a command repro."""
    expect = finding.repro.expect
    command = repro_command(finding.repro.command or "")
    res = await fresh.exec(command, timeout_s=step_s)
    output = (res.stdout or "") + (res.stderr or "")
    if res.timed_out:
        return False, "시간 초과", output, {}
    if command_did_not_run(expect, exit_code=res.exit_code, output=output, timed_out=False):
        reason = f"재현이 실행되지 않음 (명령 오류, exit {res.exit_code})"
        return False, reason, output, {"not_run": True}
    if judge_command(expect, exit_code=res.exit_code, output=output, timed_out=False):
        before = await _before_change(box, worked_in, command, None, baseline, budget)
        confirmed, reason, extra = _control_command(expect, before)
        return confirmed, reason, output, extra
    return False, "재현 명령의 결과가 기대와 달랐다", output, {}


async def _before_change(
    box: SandboxSession,
    worked_in: _Copy,
    command: str,
    test_path: str | None,
    baseline: str,
    budget: _Budget,
) -> SandboxResult | None:
    """The same repro on a copy of the tree BEFORE the change. ``None`` = no time left for it.

    Same isolation as every other copy: it is a throwaway directory driven through the box, the
    real tree is only read, it is deleted afterwards, and it spends the one shared budget."""
    if budget.remaining() <= 0:
        return None
    old = _Copy(box, f"{worked_in.root}-before-{uuid.uuid4().hex[:6]}")
    try:
        await _make_copy(box, old)
        try:
            await _to_baseline(box, old, baseline)
        except Exception as exc:
            raise _BaselineUnavailable(str(exc)) from exc
        if test_path is not None:
            await _carry_repro_file(box, worked_in, old, test_path)
        return await old.exec(command, timeout_s=_step_timeout(budget, _REPRO_TIMEOUT_S))
    finally:
        await _cleanup(box, old, budget)


def _control_test(
    before: SandboxResult | None, failed_code: int
) -> tuple[bool, str, dict[str, Any]]:
    """The repro test failed after the change: did it also fail before? → (confirmed, why, extra)."""
    if before is None:
        return False, "시간 예산이 소진되어 변경 전 대조를 하지 못했다", {}
    if before.timed_out:
        return False, "변경 전 대조 시간 초과", {}
    if judge_test(exit_code=before.exit_code, failed_code=failed_code, timed_out=False):
        return False, "변경 전에도 같은 결과", {}
    if before.exit_code == 0:
        return True, "재현 테스트가 변경 후에서만 실패했다 (변경 전에는 통과)", {}
    if repro_test_did_not_run(exit_code=before.exit_code, timed_out=False):
        # The test cannot even be collected before the change (it imports what the change adds):
        # the failure after is all there is to go on, and it is said so.
        return (
            True,
            f"재현 테스트가 변경 후에서 실패했다 (변경 전 실행 불가: 수집·import 오류, "
            f"exit {before.exit_code})",
            {"baseline_not_run": True},
        )
    return False, f"변경 전 대조를 판정하지 못했다 (exit {before.exit_code})", {}


def _control_command(
    expect: dict[str, Any], before: SandboxResult | None
) -> tuple[bool, str, dict[str, Any]]:
    """The command matched ``expect`` after the change: did it also match before?"""
    if before is None:
        return False, "시간 예산이 소진되어 변경 전 대조를 하지 못했다", {}
    if before.timed_out:
        return False, "변경 전 대조 시간 초과", {}
    out = (before.stdout or "") + (before.stderr or "")
    if command_did_not_run(expect, exit_code=before.exit_code, output=out, timed_out=False):
        return (
            False,
            "변경 전 실행 불가 — 명령형 재현은 변경 전 대조 없이 확인하지 않는다",
            {"baseline_not_run": True},
        )
    if judge_command(expect, exit_code=before.exit_code, output=out, timed_out=False):
        return False, "변경 전에도 같은 결과", {}
    return True, "재현 명령의 결과가 변경 후에서만 기대와 일치했다 (변경 전에는 불일치)", {}


async def _to_baseline(box: SandboxSession, copy: _Copy, baseline: str) -> None:
    """Turn ``copy`` (a copy of the verified state) into the tree BEFORE the change.

    Files the change ADDED are deleted — found with ``git diff --no-renames --name-only
    --diff-filter=A`` so that a renamed file's NEW path counts as added — as are untracked
    files; then every tracked file is put back as it was at ``baseline``. Ignored files (the
    project venv, installed packages) stay: the repro still needs its environment. The real
    tree is only read."""
    mount, root, ref = (
        shlex.quote(box.workspace_mount),
        shlex.quote(copy.root),
        shlex.quote(baseline),
    )
    # No pipes: each step's exit code counts (a bad ref or a shallow clone fails `git diff`, and
    # a pipe would only report the last stage). Intermediate files live outside the workspace.
    await _sh(
        box,
        f"cd {mount} && f=$(mktemp) && "
        f'git diff -z --no-renames --name-only --diff-filter=A {ref} > "$f" && '
        f'git ls-files -z -o --exclude-standard >> "$f" && '
        f'(cd {root} && xargs -0 -r rm -f -- < "$f") && '
        f'git archive {ref} -o "$f.tar" && tar -xf "$f.tar" -C {root}; '
        'rc=$?; rm -f "$f" "$f.tar"; exit $rc',
    )


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
        tail = (res.stderr or "").strip()[-300:]
        raise RuntimeError(
            f"command failed (exit {res.exit_code}): {command[:80]}"
            + (f" | stderr: {tail}" if tail else "")
        )
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


async def _snapshot(box: SandboxSession, path: str) -> None:
    """The real tree as it stands: ``.git`` (HEAD, index), uncommitted and untracked files, in a
    tarball OUTSIDE the workspace, so a restore needs nothing the verifier could touch."""
    mount = shlex.quote(box.workspace_mount)
    await _sh(box, f"cd {mount} && tar -cf {shlex.quote(path)} .")


async def _restore(box: SandboxSession, path: str) -> None:
    """Put the real tree back exactly as snapshotted: empty it, then unpack."""
    mount = shlex.quote(box.workspace_mount)
    await _sh(
        box,
        f"cd {mount} && find . -mindepth 1 -delete && tar -xf {shlex.quote(path)} -C {mount}",
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
