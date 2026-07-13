"""The ``claude_code`` executor — streams from the Claude Code CLI.

Runs ``claude --print --output-format stream-json <flags>`` as an async
subprocess, feeds the prompt on stdin, and parses the NDJSON event stream into
:class:`ExecutionChunk`s (assistant text deltas, then a terminal ``done`` /
``error``). Adapted from BSGateway's proven ``worker/executors.py`` streamer.

Robustness:

* per-line read deadline (``total_timeout_seconds``) → a terminal timeout error
  chunk, never a hung loop;
* non-zero exit / OS errors → a terminal error chunk (no crash);
* a basic rate-limit retry (re-runs the subprocess up to ``rate_limit_retries``
  times when the CLI reports a rate limit), mirroring BSGateway.

The OpenAI-API-expressible ``system`` message is forwarded via
``--append-system-prompt``; ``workspace_dir`` becomes the subprocess cwd; an
optional ``model`` becomes ``--model``. The worker's existing local ``claude``
login + harness (``CLAUDE.md``, ``settings.json``) stay in effect.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import AsyncIterator
from typing import Any

import structlog

from backend.executors.worker.claude_auth import ensure_claude_bearer
from backend.executors.worker.executors import (
    ExecutionChunk,
    _kill_process_group,
    sanitized_subprocess_env,
)

logger = structlog.get_logger(__name__)


def _subprocess_env_with_bearer() -> dict[str, str]:
    """Sanitized subprocess env plus a worker-managed ``ANTHROPIC_AUTH_TOKEN``.

    Synchronous (file IO + a possible network refresh under an flock) — call via
    :func:`asyncio.to_thread`. When no worker OAuth credential is configured /
    resolvable, returns the plain sanitized env unchanged (claude uses its own
    auth)."""
    env = sanitized_subprocess_env()
    bearer = ensure_claude_bearer()
    if bearer:
        env["ANTHROPIC_AUTH_TOKEN"] = bearer
    return env


# claude_code's JSONL stream can carry a single line past asyncio's default
# 64 KiB StreamReader limit (full file contents / diffs) — same trap as codex.
# Without a raised limit ``readline()`` raises ``LimitOverrunError`` mid-run.
_STREAM_LIMIT = 16 * 1024 * 1024

#: Headless permission settings for the confined run (see ``_build_cmd``). Auto-
#: allow Bash so the verify step (uv/pytest) runs without prompts; file edits are
#: handled by ``--permission-mode acceptEdits`` and stay confined to the cwd
#: (the per-task workspace) because the blanket bypass is no longer set.
_CONFINED_SETTINGS = json.dumps({"permissions": {"allow": ["Bash"]}})

#: A CHAT turn is a plain completion — the same thing a LiteLLM account does when
#: the caller passes no tools. Getting there takes four flags, each learned against
#: the real CLI (prod 2026-07-13, "현 프로젝트 상황 설명해줘"):
#:
#: * ``--disallowedTools "*"`` — the WILDCARD, never an enumerated list. Naming the
#:   obvious tools (Bash/Read/Edit/…) left the CLI's OTHER built-ins (ToolSearch,
#:   Skill, Workflow, Cron*, …) exposed, and the model burned 12 turns calling
#:   ToolSearch trying to go look at the project.
#: * ``--strict-mcp-config`` + an empty ``--mcp-config`` — the worker host has MCP
#:   servers configured; those are tools too.
#: * ``--setting-sources ""`` — do not load the operator's CLAUDE.md / skills. That
#:   harness belongs to an agent run, not to a chat completion.
#: * ``--system-prompt`` (REPLACE, not append; see :meth:`_build_cmd`) — Claude
#:   Code's default system prompt announces the working directory, so even with every
#:   tool denied the model still "knew" it sat in an empty temp dir and said so.
#:
#: Measured, same empty dir, same question: append + named denies → 12 turns / 44 s,
#: answering about the temp dir (44 s also blew the 45 s inline-answer budget). This
#: invocation → 1 turn / 9 s, answering from the grounding we injected.
_CHAT_FLAGS: tuple[str, ...] = (
    "--disallowedTools",
    "*",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
)


class ClaudeCodeExecutor:
    """Stream from ``claude --print --output-format stream-json``."""

    def __init__(
        self,
        timeout_seconds: int = 3600,
        total_timeout_seconds: int = 7200,
        rate_limit_retries: int = 3,
        rate_limit_wait_seconds: int = 60,
    ) -> None:
        self._cmd = self._resolve_cmd()
        self._timeout = timeout_seconds
        self._total_timeout = total_timeout_seconds
        self._rate_limit_retries = rate_limit_retries
        self._rate_limit_wait = rate_limit_wait_seconds

    @staticmethod
    def _resolve_cmd() -> str:
        resolved = shutil.which("claude")
        if resolved:
            return resolved
        if sys.platform == "win32":
            resolved = shutil.which("claude.cmd")
            if resolved:
                return resolved
        return "claude"

    def supported_task_types(self) -> list[str]:
        return ["coding", "refactor", "bugfix", "test"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        workspace = context.get("workspace_dir") or "."
        system = context.get("system") or ""
        model = context.get("model") or None
        # Absent → an agent run (back-compat: a task dispatched by an older
        # backend carries no flag, and the coding loop must never silently lose
        # its tools).
        agentic = context.get("agentic", True) is not False
        attempts_remaining = self._rate_limit_retries
        deadline = asyncio.get_event_loop().time() + self._total_timeout
        while True:
            rate_limited = False
            stderr_buf: list[str] = []
            had_delta = False
            try:
                async for chunk in self._run_once(
                    prompt, workspace, system, deadline, stderr_buf, model, agentic
                ):
                    if chunk.delta:
                        had_delta = True
                    if chunk.error and self._is_rate_limited(
                        (chunk.error or "") + "".join(stderr_buf)
                    ):
                        rate_limited = True
                        # Suppress this chunk; we may retry.
                        continue
                    yield chunk
                    if chunk.done:
                        return
                if not had_delta and self._is_rate_limited("".join(stderr_buf)):
                    rate_limited = True
            except TimeoutError:
                yield ExecutionChunk(
                    done=True,
                    error=f"Total execution timed out after {self._total_timeout}s",
                )
                return

            if rate_limited and attempts_remaining > 0:
                attempts_remaining -= 1
                logger.warning(
                    "claude_code_rate_limited",
                    attempts_remaining=attempts_remaining,
                    wait_seconds=self._rate_limit_wait,
                )
                await asyncio.sleep(self._rate_limit_wait)
                continue
            # Non-retryable failure, or retries exhausted — surface terminal error.
            yield ExecutionChunk(
                done=True,
                error="Rate limit retries exhausted" if rate_limited else "claude exited",
            )
            return

    def _build_cmd(self, system: str, model: str | None, agentic: bool = True) -> list[str]:
        # An AGENT RUN inherits the host operator's harness (CLAUDE.md / skills /
        # memory) by design — but the agent's native file writes must stay inside
        # the per-task workspace. ``--dangerously-skip-permissions`` disabled ALL
        # guards including the working-directory confinement, so an agent that
        # learned the host source-repo path from the inherited memory wrote into
        # that repo (dogfood leak). Instead: ``--permission-mode acceptEdits``
        # auto-applies edits headlessly but ONLY inside an allowed dir (the cwd =
        # the per-task clone), and the settings allow Bash so the verify step
        # (uv/pytest) still runs without re-opening writes outside the workspace.
        #
        # A CHAT TURN has no tools at all (:data:`_CHAT_DENIED_TOOLS`) — that is
        # what makes an executor account behave identically to a LiteLLM one,
        # which is BSVibe's first principle.
        cmd_args = [
            self._cmd,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if agentic:
            # An agent run: edits auto-apply headlessly but ONLY inside the cwd
            # (the per-task clone), and Bash is allowed so the verify step runs.
            cmd_args += [
                "--permission-mode",
                "acceptEdits",
                "--settings",
                _CONFINED_SETTINGS,
            ]
        else:
            # A chat turn: no tools, no MCP, no host harness (:data:`_CHAT_FLAGS`).
            cmd_args += list(_CHAT_FLAGS)
        if system:
            # An agent run APPENDS to Claude Code's harness prompt (it needs that
            # coding context). A chat turn REPLACES it — the default prompt announces
            # the cwd, which is precisely what it must not answer about.
            cmd_args += ["--append-system-prompt" if agentic else "--system-prompt", system]
        if model:
            cmd_args += ["--model", model]
        return cmd_args

    async def _run_once(
        self,
        prompt: str,
        workspace: str,
        system: str,
        deadline: float,
        stderr_buf: list[str],
        model: str | None = None,
        agentic: bool = True,
    ) -> AsyncIterator[ExecutionChunk]:
        cmd_args = self._build_cmd(system, model, agentic)
        # Inject a worker-managed OAuth bearer so a launchd-spawned claude (which
        # can't read the Keychain) authenticates instead of falling back to a
        # stale on-disk token → 401. Soft-fail + off the event loop (the helper
        # does file IO + a network refresh under an flock).
        env = await asyncio.to_thread(_subprocess_env_with_bearer)
        process: asyncio.subprocess.Process | None = None
        try:
            # Lift E15 — ``start_new_session=True`` so we can group-kill on
            # cancel (see opencode.py for the dogfood story).
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            stderr_task = asyncio.create_task(_drain(process.stderr, stderr_buf))
            # The most recent NON-``allowed`` rate_limit_event status seen on the
            # stream (e.g. ``rejected`` on a five_hour window with org-disabled
            # overage). Used only when the CLI then exits non-zero — see below.
            rate_status: str | None = None
            try:
                async for line in _aiter_lines(process.stdout, deadline):
                    parsed = _safe_json(line)
                    if parsed is None:
                        continue
                    status = _rate_limit_event_status(parsed)
                    if status is not None and status != "allowed":
                        rate_status = status
                    delta = _claude_extract_delta(parsed)
                    if delta:
                        yield ExecutionChunk(delta=delta, raw=parsed)
            except asyncio.CancelledError:
                # Lift E15 — kill the process GROUP before the inner
                # ``finally``'s ``process.wait()`` blocks for the full
                # per-task deadline. See opencode._run for the diagnosis.
                logger.info(
                    "worker_subprocess_terminate_sent",
                    pid=process.pid,
                    executor="claude_code",
                    reason="cancelled",
                )
                _kill_process_group(process)
                raise
            finally:
                rc = await asyncio.wait_for(
                    process.wait(),
                    timeout=max(0.1, deadline - asyncio.get_event_loop().time()),
                )
                await stderr_task
            yield _terminal_chunk(rc, "".join(stderr_buf), rate_status)
        except TimeoutError:
            # ``TimeoutError`` is a subclass of ``OSError`` (3.11) — re-raise so
            # ``execute`` surfaces the explicit total-timeout message rather than
            # the empty ``str(TimeoutError())`` from the OSError branch below.
            raise
        except (FileNotFoundError, PermissionError, OSError) as exc:
            yield ExecutionChunk(done=True, error=str(exc))
        finally:
            if process is not None and process.returncode is None:
                try:
                    _kill_process_group(process)
                    await process.wait()
                    logger.info("worker_subprocess_killed", pid=process.pid, executor="claude_code")
                except ProcessLookupError:  # pragma: no cover — race on shutdown
                    pass

    @staticmethod
    def _is_rate_limited(output: str) -> bool:
        lower = output.lower()
        return "hit your limit" in lower or "rate limit" in lower


# ── Stream parsing helpers ────────────────────────────────────────────────────


def _terminal_chunk(rc: int, err_text: str, rate_status: str | None) -> ExecutionChunk:
    """The final ``done`` chunk for a finished subprocess.

    A non-zero exit AFTER a non-``allowed`` rate_limit_event (the five_hour
    window hit with org-disabled overage exits 1 with NOTHING on stderr — bare
    "exit 1" otherwise) is surfaced AS a rate-limit failure, so ``_is_rate_limited``
    routes it through the wait+retry path and the founder gets an actionable
    reason instead of an opaque "claude exited"."""
    if rc == 0:
        return ExecutionChunk(done=True)
    if rate_status is not None:
        return ExecutionChunk(done=True, error=f"rate limit ({rate_status}): claude exited {rc}")
    return ExecutionChunk(done=True, error=err_text or f"exit {rc}")


def _rate_limit_event_status(event: dict[str, Any]) -> str | None:
    """The ``status`` of a ``rate_limit_event`` (``allowed`` / ``rejected`` / …),
    or ``None`` for any other event. Claude emits these on the ``stream-json``
    feed; a non-``allowed`` status that precedes a non-zero exit is how a hit
    five_hour window (with org-disabled overage) manifests — there is no stderr."""
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    status = info.get("status")
    return status if isinstance(status, str) else None


def _claude_extract_delta(event: dict[str, Any]) -> str:
    """Pull incremental assistant text from a ``stream-json`` event.

    Claude emits ``{"type": "assistant", "message": {"content": [...]}}`` blocks
    interleaved with tool calls; we surface assistant text only. Robust against
    minor schema variation (also handles a flat ``delta.text`` shape).
    """
    if event.get("type") == "assistant":
        msg = event.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
    delta = event.get("delta")
    if isinstance(delta, dict):
        text = delta.get("text") or delta.get("content")
        if isinstance(text, str):
            return text
    return ""


async def _aiter_lines(stream: asyncio.StreamReader, deadline: float) -> AsyncIterator[str]:
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError
        line = await asyncio.wait_for(stream.readline(), timeout=remaining)
        if not line:
            return
        yield line.decode("utf-8", errors="replace").rstrip("\n")


async def _drain(stream: asyncio.StreamReader, buf: list[str]) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        buf.append(chunk.decode("utf-8", errors="replace"))


def _safe_json(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["ClaudeCodeExecutor"]
