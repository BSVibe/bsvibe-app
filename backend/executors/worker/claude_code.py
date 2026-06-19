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

from backend.executors.worker.executors import (
    ExecutionChunk,
    _kill_process_group,
    sanitized_subprocess_env,
)

logger = structlog.get_logger(__name__)

# claude_code's JSONL stream can carry a single line past asyncio's default
# 64 KiB StreamReader limit (full file contents / diffs) — same trap as codex.
# Without a raised limit ``readline()`` raises ``LimitOverrunError`` mid-run.
_STREAM_LIMIT = 16 * 1024 * 1024


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
        attempts_remaining = self._rate_limit_retries
        deadline = asyncio.get_event_loop().time() + self._total_timeout
        while True:
            rate_limited = False
            stderr_buf: list[str] = []
            had_delta = False
            try:
                async for chunk in self._run_once(
                    prompt, workspace, system, deadline, stderr_buf, model
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

    def _build_cmd(self, system: str, model: str | None) -> list[str]:
        cmd_args = [
            self._cmd,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if system:
            cmd_args += ["--append-system-prompt", system]
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
    ) -> AsyncIterator[ExecutionChunk]:
        cmd_args = self._build_cmd(system, model)
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
                env=sanitized_subprocess_env(),
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
            try:
                async for line in _aiter_lines(process.stdout, deadline):
                    parsed = _safe_json(line)
                    if parsed is None:
                        continue
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
            err_text = "".join(stderr_buf)
            if rc != 0:
                yield ExecutionChunk(done=True, error=err_text or f"exit {rc}")
            else:
                yield ExecutionChunk(done=True)
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
