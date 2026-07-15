"""The ``codex`` executor — streams from the OpenAI Codex CLI.

Runs ``codex exec --json`` as an async subprocess, feeds the prompt on stdin,
and parses its item-based JSONL event stream into :class:`ExecutionChunk`s
(assistant text, then a terminal ``done`` / ``error``). Adapted from
BSGateway's proven ``worker/executors.py`` streamer, mirroring the worker's
``claude_code`` executor.

Robustness:

* per-line read deadline (``timeout_seconds``) → a terminal timeout error chunk,
  never a hung loop;
* non-zero exit / OS errors → a terminal error chunk (no crash);
* ``TimeoutError`` is caught BEFORE the ``OSError`` branch (``TimeoutError``
  subclasses ``OSError`` in 3.11) so the explicit timeout message is surfaced
  rather than an empty ``str(OSError())``.

Flags track codex-cli's current contract:

* ``exec --json`` — non-interactive run with the JSONL event stream;
* ``--skip-git-repo-check`` — the per-task workspace is a fresh empty temp dir
  (no git repo, untrusted), which codex otherwise refuses to run in;
* ``--sandbox workspace-write`` — the supported sandbox policy;
* ``--config model_instructions_file=<path>`` — the system-message override
  (the old ``experimental_instructions_file`` key silently drops it);
* ``--model`` — per-run model override.

The prompt is fed on stdin (codex reads stdin when no positional prompt is
given). ``workspace_dir`` becomes the subprocess cwd; the system message (if
any) is written to a temp file removed when the subprocess exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import structlog

from backend.executors.worker.executors import (
    ExecutionChunk,
    _kill_process_group,
    sanitized_subprocess_env,
)

logger = structlog.get_logger(__name__)

# ``codex exec --json`` emits one JSON event per line, and a single line can
# carry full file contents / diffs / reasoning — easily past asyncio's default
# 64 KiB StreamReader limit. Without a raised limit ``readline()`` raises
# ``LimitOverrunError`` ("Separator is found, but chunk is longer than limit")
# mid-run and the task dies. 16 MiB comfortably covers a large multi-file turn.
_STREAM_LIMIT = 16 * 1024 * 1024


class CodexExecutor:
    """Stream from ``codex exec --json``."""

    def __init__(self, timeout_seconds: int = 3600) -> None:
        self._cmd = shutil.which("codex") or "codex"
        self._timeout = timeout_seconds

    def supported_task_types(self) -> list[str]:
        return ["codex"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        workspace = context.get("workspace_dir") or "."
        system = context.get("system") or ""
        model = context.get("model") or None
        # INV-7 #6 — a chat turn (frame/judge/ingest: no BSVibe tools) must run with the
        # executor's OWN tools OFF and answer in a single turn. ``codex exec`` cannot: its
        # shell/exec tool is intrinsic and NO flag disables it (verified against codex 0.130.0's
        # own binary — the knobs are ``--sandbox``, ``--ask-for-approval``, ``--model``,
        # ``--config``; ``--sandbox read-only`` only blocks WRITES, the model still runs read
        # commands to explore). Left agentic, it would inspect the empty per-task temp dir and
        # answer "the directory is empty" — the exact bug this invariant kills. With no honest
        # tools-off mode, REFUSE loudly (mirroring the adapter's ``ExecutorAdapterUnavailable``
        # T2a refusal) rather than silently run agentic. Absent key → agent run (back-compat:
        # a task from an older backend carries no flag, and a coding loop must never silently
        # lose its tools).
        agentic = context.get("agentic", True) is not False
        if not agentic:
            yield ExecutionChunk(
                done=True,
                error=(
                    "codex cannot serve a chat turn: `codex exec` has no mode that disables its "
                    "shell/exec tool, so it would inspect its empty workspace instead of "
                    "answering from the prompt. Route chat-shaped work to an executor that can "
                    "turn its tools off (claude_code, opencode) or to a LiteLLM account."
                ),
            )
            return
        deadline = asyncio.get_event_loop().time() + self._timeout

        sys_path: str | None = _write_system_file(system) if system else None
        cmd_args = self._build_cmd(sys_path, model)
        try:
            async for chunk in self._run(cmd_args, prompt, workspace, deadline):
                yield chunk
        finally:
            if sys_path:
                try:
                    os.unlink(sys_path)
                except OSError:  # pragma: no cover — best-effort cleanup
                    pass

    async def _run(
        self,
        cmd_args: list[str],
        prompt: str,
        workspace: str,
        deadline: float,
    ) -> AsyncIterator[ExecutionChunk]:
        process: asyncio.subprocess.Process | None = None
        stderr_buf: list[str] = []
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
                    delta = _codex_extract_delta(parsed)
                    if delta:
                        yield ExecutionChunk(delta=delta, raw=parsed)
            except asyncio.CancelledError:
                # Lift E15 — kill the process GROUP before the inner
                # ``finally``'s ``process.wait()`` blocks for the full
                # per-task deadline. See opencode._run for the diagnosis.
                logger.info(
                    "worker_subprocess_terminate_sent",
                    pid=process.pid,
                    executor="codex",
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
            # ``TimeoutError`` is a subclass of ``OSError`` (3.11) — handle it
            # BEFORE the ``OSError`` branch so the explicit timeout message is
            # surfaced rather than the empty ``str(OSError())`` below.
            yield ExecutionChunk(done=True, error=f"Execution timed out after {self._timeout}s")
        except (FileNotFoundError, PermissionError, OSError) as exc:
            yield ExecutionChunk(done=True, error=str(exc))
        finally:
            if process is not None and process.returncode is None:
                try:
                    _kill_process_group(process)
                    await process.wait()
                    logger.info("worker_subprocess_killed", pid=process.pid, executor="codex")
                except ProcessLookupError:  # pragma: no cover — race on shutdown
                    pass

    def _build_cmd(self, sys_path: str | None, model: str | None) -> list[str]:
        # The worker runs each task in a fresh, EMPTY temp dir — never a git repo
        # and never in codex's trusted-projects list. Without --skip-git-repo-check
        # the CLI refuses ("Not inside a trusted directory") and writes nothing.
        cmd_args = [
            self._cmd,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
        ]
        if sys_path:
            cmd_args += ["--config", f"model_instructions_file={sys_path}"]
        if model:
            cmd_args += ["--model", model]
        return cmd_args


# ── Stream parsing helpers ────────────────────────────────────────────────────


def _codex_extract_delta(event: dict[str, Any]) -> str:
    """Pull assistant text from a ``codex exec --json`` JSONL event.

    codex-cli emits an item-based stream: ``thread.started`` → ``turn.started``
    → ``item.*`` → ``turn.completed``. The assistant's answer arrives whole as
    ``{"type": "item.completed", "item": {"type": "agent_message", "text": ...}}``
    — there is no token-level delta event, so we surface the text from the
    completed ``agent_message`` item only.
    """
    if event.get("type") == "item.completed":
        item = event.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text") or ""
            return text if isinstance(text, str) else ""
    return ""


def _write_system_file(system: str) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
    try:
        tmp.write(system)
    finally:
        tmp.close()
    return tmp.name


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


__all__ = ["CodexExecutor"]
