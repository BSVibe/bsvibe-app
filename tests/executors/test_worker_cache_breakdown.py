"""#1104 — an executor turn's cache breakdown is stored, not just its weighted sum.

Measured 2026-09-30: run ``92b76fba``'s Claude Code task metered 3,055,575 input
tokens — one number, the raw ``input + cache_creation + cache_read`` sum. Weighting
(a cache read is a tenth, a write 1.25× / 2×) and the in-session budget have since
landed, but the row still keeps ONE figure: afterwards nobody can tell how much of
a task was plain input, cache reads or cache writes, so its real cost cannot be
recomputed — and neither can a weight that turns out wrong.

These tests pin the breakdown at every hop: the CLI's ``result`` event → the
terminal chunk → the drain → the result POST → ``record_result`` → the row.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import backend.executors.db  # noqa: F401
from backend.executors import dispatch
from backend.executors.db import ExecutorTaskRow
from backend.executors.worker.executors import ExecutionChunk, UsageBreakdown

from .._support import memory_session

_RESULT = {
    "type": "result",
    "subtype": "success",
    "usage": {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_creation_input_tokens": 9,
        "cache_read_input_tokens": 700,
        "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 6},
    },
}


def test_claude_code_reads_the_breakdown_off_its_result_event() -> None:
    from backend.executors.worker.claude_code import _claude_usage_breakdown

    assert _claude_usage_breakdown(_RESULT) == UsageBreakdown(
        input_tokens=11, cache_read_tokens=700, cache_write_5m_tokens=3, cache_write_1h_tokens=6
    )


def test_an_unsplit_cache_write_is_counted_as_the_5m_write() -> None:
    """The same assumption the weighting makes — the cheaper write, never a guess at the dearer."""
    from backend.executors.worker.claude_code import _claude_usage_breakdown

    event = {"type": "result", "usage": {"input_tokens": 1, "cache_creation_input_tokens": 8}}
    assert _claude_usage_breakdown(event) == UsageBreakdown(
        input_tokens=1, cache_read_tokens=0, cache_write_5m_tokens=8, cache_write_1h_tokens=0
    )


def test_an_event_without_usage_has_no_breakdown() -> None:
    from backend.executors.worker.claude_code import _claude_usage_breakdown

    assert _claude_usage_breakdown({"type": "assistant", "message": {}}) is None
    assert _claude_usage_breakdown({"type": "result"}) is None


def test_the_stream_scrape_keeps_the_breakdown_for_the_terminal_chunk() -> None:
    from backend.executors.worker.claude_code import _StreamScrape, _terminal_chunk

    scrape = _StreamScrape()
    scrape.feed(_RESULT)
    scrape.feed({"type": "assistant", "message": {}})  # a later non-result keeps it
    chunk = _terminal_chunk(0, "", scrape)
    assert chunk.usage_breakdown == UsageBreakdown(11, 700, 3, 6)
    assert chunk.error is None
    # A failed turn spent the same tokens — the breakdown rides it too.
    failed = _terminal_chunk(1, "boom", scrape)
    assert failed.usage_breakdown == UsageBreakdown(11, 700, 3, 6)
    assert failed.error == "boom"


@pytest.mark.asyncio
async def test_the_drain_carries_the_breakdown_to_the_result_post() -> None:
    from backend.executors.worker.main import _result_payload, _stream_and_collect

    class _Executor:
        async def execute(self, prompt: str, context: dict[str, Any]) -> Any:
            yield ExecutionChunk(delta="hi")
            yield ExecutionChunk(
                done=True,
                usage_prompt_tokens=100,
                usage_completion_tokens=5,
                usage_breakdown=UsageBreakdown(11, 700, 3, 6),
            )

    outcome = await _stream_and_collect(
        executor=_Executor(),
        prompt="p",
        context={},
        stream_chan="c",
        redis=None,
        task_id="t",
    )
    payload = _result_payload("t", outcome)
    assert payload["usage_input_tokens"] == 11
    assert payload["usage_cache_read_tokens"] == 700
    assert payload["usage_cache_write_5m_tokens"] == 3
    assert payload["usage_cache_write_1h_tokens"] == 6
    assert payload["usage_prompt_tokens"] == 100

    # And the body the backend validates accepts exactly that payload.
    from backend.api.v1.workers import WorkerResultBody

    body = WorkerResultBody.model_validate({**payload, "task_id": str(uuid.uuid4())})
    assert body.usage_cache_read_tokens == 700


def test_an_old_workers_body_still_validates_without_the_breakdown() -> None:
    from backend.api.v1.workers import WorkerResultBody

    legacy = WorkerResultBody(task_id=uuid.uuid4(), success=True, output="x")
    assert legacy.usage_input_tokens == 0
    assert legacy.usage_cache_write_1h_tokens == 0


def test_a_negative_breakdown_is_refused() -> None:
    from pydantic import ValidationError

    from backend.api.v1.workers import WorkerResultBody

    with pytest.raises(ValidationError):
        WorkerResultBody(task_id=uuid.uuid4(), success=True, usage_cache_read_tokens=-1)


@pytest.mark.asyncio
async def test_record_result_stores_the_breakdown_on_the_row() -> None:
    import fakeredis.aioredis as fakeredis_aio

    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    workspace_id, worker_id = uuid.uuid4(), uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.flush()
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)
        await s.commit()

        await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            worker_id=worker_id,
            success=True,
            output="done",
            error_message=None,
            usage_prompt_tokens=100,
            usage_completion_tokens=5,
            usage_input_tokens=11,
            usage_cache_read_tokens=700,
            usage_cache_write_5m_tokens=3,
            usage_cache_write_1h_tokens=6,
        )
        await s.commit()

        row = await s.get(ExecutorTaskRow, task.id)
        assert row is not None
        assert (
            row.usage_input_tokens,
            row.usage_cache_read_tokens,
            row.usage_cache_write_5m_tokens,
            row.usage_cache_write_1h_tokens,
        ) == (11, 700, 3, 6)
