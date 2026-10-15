"""A session the backend stops still reports what it spent (#1158, #928 잔여).

When the backend gives up on an executor turn — its own timeout, or the run cancelled
mid-turn (#1106) — it closes the task row ``failed`` and XADDs ``cancel``. The worker's
``CancelledError`` branch used to skip the result POST entirely, so every token the session
had already burned was reported nowhere. The worker held the number: Claude Code's in-session
tally (``_SessionLimits``, #1104) folds every assistant message's usage while the session runs.

The same hole sat on the worker's own deadline (#965): ``_report_deadline_exceeded`` posted a
``failed`` result with no usage at all.

The backend side already exists — ``record_result`` takes usage onto a ``failed`` row that
holds none (#928) and refuses its status and output. So the report is an ordinary ``failed``
result; there is nothing new for the backend to learn.

These run the REAL :class:`ClaudeCodeExecutor` (on a fake subprocess) through the REAL
:func:`handle_task`: the wire under test runs from the stream parser to the POST body, and a
handler that forgot to hand the executor its tally fails here, not only a unit that never
sees one.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from backend.api.v1.workers import WorkerResultBody
from backend.executors.worker import main as worker_main
from backend.executors.worker.claude_code import ClaudeCodeExecutor
from tests.executors.worker.test_claude_code import _agent_ctx, _FakeProcess, _patch_subprocess
from tests.executors.worker.test_claude_code_session_limits import _assistant_usage


class _ThenHang:
    """A stdout that hands out ``lines`` and then blocks — a session still working."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.drained = asyncio.Event()

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self.drained.set()
        await asyncio.sleep(3600)
        return b""  # pragma: no cover

    async def read(self, n: int = -1) -> bytes:
        return b""


def _session(monkeypatch: pytest.MonkeyPatch, lines: list[bytes]) -> _ThenHang:
    proc = _FakeProcess(stdout_lines=[])
    stdout = _ThenHang(lines)
    proc.stdout = stdout  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc)
    return stdout


def _transport(results: list[dict[str, Any]]) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workers/result":
            results.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    return httpx.MockTransport(_handler)


def _task(**extra: Any) -> dict[str, Any]:
    ctx = _agent_ctx()
    task_id = str(uuid.uuid4())
    task: dict[str, Any] = {
        "task_id": task_id,
        "executor_type": "claude_code",
        "prompt": "work",
        "system": "",
        "workspace_dir": ".",
        "stream_channel": f"task:{task_id}:stream",
        "done_channel": f"task:{task_id}:done",
        "action": "execute",
        "agentic": "1",
        "mcp_config": ctx["mcp_config"],
        "allowed_tools": " ".join(ctx["allowed_tools"]),
    }
    task.update(extra)
    return task


async def _handle(task: dict[str, Any], results: list[dict[str, Any]], tmp_path: Any) -> None:
    async with httpx.AsyncClient(transport=_transport(results), base_url="http://test") as client:
        await worker_main.handle_task(
            task,
            executors={"claude_code": ClaudeCodeExecutor()},
            client=client,
            headers={"X-Worker-Token": "T"},
            redis=None,
            sandbox_cwd=str(tmp_path),
        )


# m1 weighs 100 + ceil(9000×0.1) = 1000 in, 7 out; m2 weighs 50 + 2×200 + 0 = 450 in, 3 out.
_TWO_MESSAGES = [
    _assistant_usage("m1", inp=100, write_1h=0, read=9000, out=7),
    _assistant_usage("m1", inp=100, write_1h=0, read=9000, out=7, block="text"),
    _assistant_usage("m2", inp=50, write_1h=200, read=0, out=3),
]


async def test_a_cancelled_session_posts_what_it_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    stdout = _session(monkeypatch, _TWO_MESSAGES)
    results: list[dict[str, Any]] = []

    running = asyncio.create_task(_handle(_task(), results, tmp_path))
    await asyncio.wait_for(stdout.drained.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert len(results) == 1, "the cancelled session must report once — and only once"
    body = results[0]
    assert body["success"] is False
    assert body["output"] == ""
    assert (body["usage_prompt_tokens"], body["usage_completion_tokens"]) == (1450, 10)
    # The raw split behind the weighted figure (#1104), summed over the two messages.
    assert body["usage_input_tokens"] == 150
    assert body["usage_cache_read_tokens"] == 9000
    assert body["usage_cache_write_1h_tokens"] == 200
    assert body["usage_cache_write_5m_tokens"] == 0


async def test_a_session_cancelled_before_any_usage_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Control: nothing spent, nothing to say — the Lift E14 rule (skip the POST) still holds
    when there is no number to carry."""
    stdout = _session(monkeypatch, [])
    results: list[dict[str, Any]] = []

    running = asyncio.create_task(_handle(_task(), results, tmp_path))
    await asyncio.wait_for(stdout.drained.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert results == []


async def test_a_session_past_the_workers_deadline_posts_what_it_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _session(monkeypatch, _TWO_MESSAGES)
    results: list[dict[str, Any]] = []

    await _handle(_task(timeout_s="0.5"), results, tmp_path)

    assert len(results) == 1
    body = results[0]
    assert body["success"] is False
    assert "gave up" in body["error_message"]
    assert (body["usage_prompt_tokens"], body["usage_completion_tokens"]) == (1450, 10)


async def test_the_report_survives_a_backend_refusal_without_swallowing_the_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A refused or failed POST must not turn a cancel into something else: the handler still
    ends CANCELLED, which is what lets ``run_once`` drop it from ``in_flight``."""
    stdout = _session(monkeypatch, _TWO_MESSAGES)

    def _refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    async def _run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_refuse), base_url="http://test"
        ) as client:
            await worker_main.handle_task(
                _task(),
                executors={"claude_code": ClaudeCodeExecutor()},
                client=client,
                headers={"X-Worker-Token": "T"},
                redis=None,
                sandbox_cwd=str(tmp_path),
            )

    running = asyncio.create_task(_run())
    await asyncio.wait_for(stdout.drained.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_the_backend_accepts_the_report_as_it_is_posted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Seam: the body the worker sends parses under the backend's ``extra="forbid"`` model.
    A field the backend does not know would 422 the report and lose the number again."""
    stdout = _session(monkeypatch, _TWO_MESSAGES)
    results: list[dict[str, Any]] = []

    running = asyncio.create_task(_handle(_task(), results, tmp_path))
    await asyncio.wait_for(stdout.drained.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    parsed = WorkerResultBody.model_validate(results[0])
    assert parsed.usage_prompt_tokens == 1450
