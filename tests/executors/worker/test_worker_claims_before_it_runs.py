"""The worker must claim a task before executing it (#965).

``run_once`` currently goes straight from the poll response to
``in_flight.add(asyncio.create_task(_run(task)))``. ``_RUNNING_TASKS`` is
consulted **only** in the cancel branch, so the same ``task_id`` arriving twice
is executed twice — measured directly on ``backend/executors/worker/main.py``.

That is the constraint every redelivery design has to live with, and it is why
redelivery cannot be bolted onto the backend alone. Two rungs are asserted here:

1. **claim** — the worker asks the backend for the task before running it, and
   a refusal (someone already has it / it is terminal) means it does **not**
   run. This is the rung that makes redelivery safe;
2. **local dedupe** — a duplicate arriving while the first copy is still in
   flight is dropped on the worker's own floor, without a round trip. Defence in
   depth: rung 1 already covers it, but rung 1 lives across a network and this
   one does not.

The claim is deliberately *fail-closed on refusal, fail-open on error*: a
refusal is a positive answer ("not yours to run"), whereas a transport failure
says nothing about ownership, and treating it as a refusal would turn a
backend blip into silently dropped work — the same
degraded-read-becomes-a-measurement trap the platform has been bitten by before.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RecordingClient:
    """Minimal ``httpx.AsyncClient`` stand-in for ``run_once``.

    ``claim_answers`` maps task_id → what ``/claim`` should reply. A task_id
    absent from the map is claimed successfully (the common path).
    """

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        *,
        claim_answers: dict[str, bool] | None = None,
    ) -> None:
        self._tasks = tasks
        self._claim_answers = claim_answers or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        if url.endswith("/heartbeat"):
            return _FakeResponse({"status": "ok"})
        if url.endswith("/poll"):
            tasks, self._tasks = self._tasks, []
            return _FakeResponse(tasks)
        if url.endswith("/claim"):
            task_id = str((kwargs.get("json") or {}).get("task_id"))
            return _FakeResponse({"claimed": self._claim_answers.get(task_id, True)})
        raise AssertionError(f"unexpected POST {url}")

    def claimed_ids(self) -> list[str]:
        return [
            str((kw.get("json") or {}).get("task_id"))
            for url, kw in self.calls
            if url.endswith("/claim")
        ]


def _task_payload(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "executor_type": "claude_code",
        "prompt": "p",
        "system": "",
        "workspace_dir": ".",
        "stream_channel": f"task:{task_id}:stream",
        "done_channel": f"task:{task_id}:done",
        "action": "execute",
        "agentic": "0",
        "execution_target": "server_sandbox",
    }


async def _run_once_with(
    monkeypatch: pytest.MonkeyPatch,
    client: _RecordingClient,
    *,
    executed: list[str],
    block: asyncio.Event | None = None,
    in_flight: set[asyncio.Task[None]] | None = None,
) -> set[asyncio.Task[None]]:
    from backend.executors.worker import main as worker_main
    from backend.executors.worker.config import WorkerSettings

    async def _fake_handle_task(task: dict[str, Any], **_kw: Any) -> None:
        executed.append(str(task["task_id"]))
        if block is not None:
            await block.wait()

    monkeypatch.setattr(worker_main, "handle_task", _fake_handle_task)

    settings = WorkerSettings(name="t", server_url="http://x", max_parallel_tasks=3)
    return await worker_main.run_once(
        client=client,
        settings=settings,
        executors={"claude_code": object()},
        redis=None,
        headers={"X-Worker-Token": "tok"},
        in_flight=in_flight if in_flight is not None else set(),
    )


async def test_the_worker_claims_before_executing(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = str(uuid.uuid4())
    client = _RecordingClient([_task_payload(task_id)])
    executed: list[str] = []

    in_flight = await _run_once_with(monkeypatch, client, executed=executed)
    await asyncio.gather(*in_flight, return_exceptions=True)

    assert client.claimed_ids() == [task_id], (
        "the worker ran without recording receipt — an unclaimed dispatched row "
        "is exactly the state redelivery keys off, so this would make every "
        "running task look lost"
    )
    assert executed == [task_id]


async def test_a_refused_claim_means_the_worker_does_not_run_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rung that makes redelivery safe against the worker's missing dedupe."""
    task_id = str(uuid.uuid4())
    client = _RecordingClient([_task_payload(task_id)], claim_answers={task_id: False})
    executed: list[str] = []

    in_flight = await _run_once_with(monkeypatch, client, executed=executed)
    await asyncio.gather(*in_flight, return_exceptions=True)

    assert executed == [], (
        "a refused claim means another copy already owns this task; running it "
        "anyway is the duplicate execution the whole design exists to prevent"
    )


async def test_a_duplicate_arriving_mid_flight_is_dropped_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth — no round trip needed to reject a task already running."""
    task_id = str(uuid.uuid4())
    block = asyncio.Event()
    executed: list[str] = []

    first_client = _RecordingClient([_task_payload(task_id)])
    in_flight = await _run_once_with(monkeypatch, first_client, executed=executed, block=block)
    await asyncio.sleep(0)  # let the wrapper task reach handle_task

    second_client = _RecordingClient([_task_payload(task_id)])
    in_flight = await _run_once_with(
        monkeypatch,
        second_client,
        executed=executed,
        block=block,
        in_flight=in_flight,
    )
    # Without this the duplicate's wrapper Task has not been scheduled yet and
    # ``executed`` cannot grow — the assertion below would pass no matter what
    # the implementation did. Give the loop the chance to run the duplicate.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert second_client.claimed_ids() == [], (
        "a task already in flight here must be rejected on the worker's own "
        "floor, without asking the backend"
    )
    assert executed == [task_id], f"executed twice: {executed}"

    block.set()
    await asyncio.gather(*in_flight, return_exceptions=True)


async def test_a_claim_transport_error_does_not_silently_drop_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open on ERROR, fail-closed on REFUSAL — they are not the same answer.

    A 500/timeout says nothing about who owns the task. Degrading it to "refused"
    would let one backend blip discard real work with no record anywhere.
    """
    task_id = str(uuid.uuid4())

    class _BrokenClaimClient(_RecordingClient):
        async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            if url.endswith("/claim"):
                self.calls.append((url, kwargs))
                raise RuntimeError("connection reset")
            return await super().post(url, **kwargs)

    client = _BrokenClaimClient([_task_payload(task_id)])
    executed: list[str] = []

    in_flight = await _run_once_with(monkeypatch, client, executed=executed)
    await asyncio.gather(*in_flight, return_exceptions=True)

    assert executed == [task_id], (
        "a transport failure on the claim must not be read as 'someone else has it'"
    )
