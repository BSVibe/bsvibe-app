"""The executor path must report LLM token usage — 게이트 1 후속.

#911 gave every run a token meter (``execution_runs.usage_*``) and a runaway
ceiling (``agent_max_run_tokens``, 2M → ``run_token_cap_reached`` Decision).
Both were wired onto ONE producer: the native LiteLLM turn, whose adapter
carries usage through :func:`_from_llm_response`.

The OTHER producer — a run executed by a registered host worker (the coding-agent
CLIs: ``claude_code`` / ``codex`` / ``opencode``) — reported nothing, and four
links of the chain were each independently missing:

1. the worker's executors never read usage off the CLI's own stream,
2. ``executor_tasks`` had no column to carry it,
3. ``POST /api/v1/workers/result`` (``extra="forbid"``) had no field to accept it,
4. ``ExecutorAdapter.chat`` built ``ChatResponse(content=...)`` — usage defaulting
   to 0 — and ``loop_llm``'s ``getattr(response, "usage_prompt_tokens", 0)``
   laundered that absence into a *measurement* of zero.

So an executor run metered 0 tokens forever and the ceiling could never fire on
it — on exactly the runaway class it exists for (a CLI agent looping its 48 work
turns). And since a tenant gets executor capacity only by registering its own
worker, the ceiling was inert for every workspace but the founder's native runs.

These tests pin each link, and — decisively — the end-to-end proposition that an
executor turn's tokens reach the run's meter. A test that only asserted "the
column exists" would stay green on a chain still broken one hop later.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

# Importing the module db registers the tables on the shared Base.metadata so
# ``memory_session``'s create_all materialises them.
import backend.executors.db  # noqa: F401
from backend.executors import dispatch
from backend.executors.db import ExecutorTaskRow
from backend.executors.worker.executors import ExecutionChunk

from .._support import memory_session


async def _make_redis() -> Any:
    """Return a connected fakeredis client, or skip when unavailable."""
    try:
        import fakeredis.aioredis as fakeredis_aio  # noqa: PLC0415
    except ImportError:  # pragma: no cover - fakeredis is a declared test dep
        pytest.skip("fakeredis not installed")
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _dispatched_task(s, redis, workspace_id, worker_id):
    """A task in the state a worker legitimately closes: dispatched to ``worker_id``."""
    task = await dispatch.create_task(
        s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
    )
    await s.flush()
    await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)
    await s.commit()
    return task


# ── Link 1: the executors read usage off their own CLI stream ────────────────


def test_execution_chunk_carries_usage() -> None:
    """The worker's chunk contract has somewhere to put a turn's usage.

    Defaults are 0 so every existing executor and test double stays valid; an
    executor that knows its usage sets it on the terminal chunk.
    """
    chunk = ExecutionChunk(done=True, usage_prompt_tokens=120, usage_completion_tokens=34)
    assert chunk.usage_prompt_tokens == 120
    assert chunk.usage_completion_tokens == 34
    assert ExecutionChunk().usage_prompt_tokens == 0


def test_claude_code_extracts_usage_from_its_result_event() -> None:
    """``claude --print --output-format stream-json`` already emits the numbers.

    The final ``{"type": "result", ..., "usage": {...}}`` event carries the
    turn's token counts; the worker's line loop parsed every event and threw
    this one away. Cache reads/creations are input tokens the account is billed
    for, so they count toward the ceiling too.
    """
    from backend.executors.worker.claude_code import _claude_extract_usage

    usage = _claude_extract_usage(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
            },
        }
    )
    assert usage == (11 + 5 + 7, 22)


def test_claude_code_ignores_events_that_are_not_the_result() -> None:
    """A non-result event yields nothing — never a zero that looks measured."""
    from backend.executors.worker.claude_code import _claude_extract_usage

    assert _claude_extract_usage({"type": "assistant", "message": {"content": []}}) is None
    assert _claude_extract_usage({"type": "result"}) is None


def test_codex_extracts_usage_from_its_turn_completed_event() -> None:
    """Codex reports its usage on ``turn.completed``."""
    from backend.executors.worker.codex import _codex_extract_usage

    usage = _codex_extract_usage(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 40,
                "cached_input_tokens": 10,
                "output_tokens": 9,
            },
        }
    )
    assert usage == (50, 9)
    assert _codex_extract_usage({"type": "item.completed", "item": {}}) is None


def test_opencode_extracts_usage_from_its_message_info() -> None:
    """opencode's message response carries ``info.tokens``."""
    from backend.executors.worker.opencode import _opencode_extract_usage

    usage = _opencode_extract_usage(
        {
            "info": {
                "tokens": {
                    "input": 80,
                    "output": 12,
                    "reasoning": 3,
                    "cache": {"read": 4, "write": 6},
                }
            }
        }
    )
    # Reasoning tokens are billed as output; cache read/write as input.
    assert usage == (80 + 4 + 6, 12 + 3)
    assert _opencode_extract_usage({"info": {}}) is None


# ── Link 2 + 3: the task row and the result body carry it ────────────────────


@pytest.mark.asyncio
async def test_record_result_persists_the_reported_usage() -> None:
    """A worker's reported usage lands on the task row.

    The row is where ``ExecutorAdapter`` reads a completed turn from, so a
    number that stops here never reaches the run's meter.
    """
    workspace_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await _dispatched_task(s, redis, workspace_id, worker_id)
        updated = await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            worker_id=worker_id,
            success=True,
            output="done",
            error_message=None,
            usage_prompt_tokens=210,
            usage_completion_tokens=17,
        )
        assert updated is not None
        await s.commit()

        row = await s.get(ExecutorTaskRow, task.id)
        assert row is not None
        assert row.usage_prompt_tokens == 210
        assert row.usage_completion_tokens == 17


@pytest.mark.asyncio
async def test_record_result_defaults_usage_to_zero_for_an_old_worker() -> None:
    """A worker that predates the field still closes its task.

    Backend deploys before the host workers are kickstarted, so for one window
    the old worker posts a body without usage. That must record a task, not a
    422 that strands the run.
    """
    workspace_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await _dispatched_task(s, redis, workspace_id, worker_id)
        updated = await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            worker_id=worker_id,
            success=True,
            output="done",
            error_message=None,
        )
        assert updated is not None
        assert updated.usage_prompt_tokens == 0
        assert updated.usage_completion_tokens == 0


def test_worker_result_body_accepts_usage() -> None:
    """``extra="forbid"`` means the field must be declared to be postable."""
    from backend.api.v1.workers import WorkerResultBody

    body = WorkerResultBody(
        task_id=uuid.uuid4(),
        success=True,
        output="x",
        usage_prompt_tokens=5,
        usage_completion_tokens=6,
    )
    assert body.usage_prompt_tokens == 5
    assert body.usage_completion_tokens == 6

    # And a pre-field worker's body still validates.
    legacy = WorkerResultBody(task_id=uuid.uuid4(), success=True, output="x")
    assert legacy.usage_prompt_tokens == 0


# ── Link 4: the adapter carries the row's usage onto the turn ────────────────


def test_chat_response_from_a_completed_executor_task_carries_usage() -> None:
    """The executor turn is shaped like a LiteLLM completion — usage included.

    This is the hop that made every earlier link pointless: ``ExecutorAdapter``
    returned ``ChatResponse(content=completed.output or "")``, so the dataclass
    defaults zeroed a number the row already held.
    """
    from backend.dispatch.adapter import _chat_response_from_task

    task = ExecutorTaskRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        prompt="p",
        status="done",
        output="the answer",
        usage_prompt_tokens=333,
        usage_completion_tokens=44,
    )
    response = _chat_response_from_task(task)
    assert response.content == "the answer"
    assert response.usage_prompt_tokens == 333
    assert response.usage_completion_tokens == 44


# ── The chain, end to end: an executor turn's tokens reach the run's meter ────


@pytest.mark.asyncio
async def test_an_executor_turn_meters_its_tokens_onto_the_loop_turn() -> None:
    """The proposition all the links exist for.

    ``ResolverLoopLlm.complete`` is the hop that hands the drive loop a
    ``LoopTurn``; ``_drive_loop`` then accumulates ``LoopTurn.usage_*`` onto the
    run and enforces ``agent_max_run_tokens`` (already proven end-to-end by
    ``test_run_accumulates_llm_token_usage`` / ``test_run_stops_on_token_cap_with_a_decision``).

    So proving that an EXECUTOR-completed task arrives here with its tokens
    intact closes the chain against the run's meter and its ceiling. Before this
    fix the turn arrived as ``(0, 0)`` no matter what the CLI had spent, which
    is why the ceiling never fired on a worker-executed run.
    """
    from backend.dispatch.adapter import _chat_response_from_task
    from backend.workflow.application.loop_llm import ResolverLoopLlm

    completed = ExecutorTaskRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        prompt="p",
        status="done",
        output="the agent's answer",
        usage_prompt_tokens=1_400_000,
        usage_completion_tokens=650_000,
    )

    class _ExecutorAdapterDouble:
        """Stands in for ``ExecutorAdapter.chat``'s return path only."""

        async def chat(self, **_: Any) -> Any:
            return _chat_response_from_task(completed)

    turn = await ResolverLoopLlm(adapter=_ExecutorAdapterDouble()).complete(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=None,
    )

    assert turn.content == "the agent's answer"
    assert turn.usage_prompt_tokens == 1_400_000
    assert turn.usage_completion_tokens == 650_000
    # Two such turns cross the 2M default ceiling — the runaway this exists to stop.
    assert (turn.usage_prompt_tokens + turn.usage_completion_tokens) * 2 > 2_000_000


@pytest.mark.asyncio
async def test_the_drain_loop_carries_executor_usage_to_the_result_post() -> None:
    """The worker's own hop: chunk usage → ``_StreamOutcome`` → result POST body.

    Pins that the terminal chunk's numbers survive the drain, and that a stream
    which reported NOTHING is flagged (``reported_usage=False``) rather than
    passed off as a metered zero.
    """
    from backend.executors.worker.main import _stream_and_collect

    class _Executor:
        def __init__(self, chunks: list[ExecutionChunk]) -> None:
            self._chunks = chunks

        async def execute(self, prompt: str, context: dict[str, Any]) -> Any:
            for chunk in self._chunks:
                yield chunk

    metered = await _stream_and_collect(
        executor=_Executor(
            [
                ExecutionChunk(delta="hello"),
                ExecutionChunk(done=True, usage_prompt_tokens=900, usage_completion_tokens=80),
            ]
        ),
        prompt="p",
        context={},
        stream_chan="c",
        redis=None,
        task_id=str(uuid.uuid4()),
        local_workspace=None,
        cleanup_workspace=False,
    )
    assert metered.usage_prompt_tokens == 900
    assert metered.usage_completion_tokens == 80
    assert metered.reported_usage is True

    silent = await _stream_and_collect(
        executor=_Executor([ExecutionChunk(delta="hello"), ExecutionChunk(done=True)]),
        prompt="p",
        context={},
        stream_chan="c",
        redis=None,
        task_id=str(uuid.uuid4()),
        local_workspace=None,
        cleanup_workspace=False,
    )
    assert silent.usage_prompt_tokens == 0
    # The distinction that keeps a re-opened hole audible instead of silent.
    assert silent.reported_usage is False
