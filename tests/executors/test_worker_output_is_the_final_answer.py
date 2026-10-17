"""#949 item 2 — an agentic turn's output is the CLI's final answer, not its narration.

``claude --print --output-format stream-json`` streams every assistant text block of a
multi-step session ("Let me look at…", "Now I'll run the tests…") and closes with ONE
``{"type": "result", "result": "<final answer>"}`` event. The worker joined every streamed
block into ``output``; the ``result`` line was read for its usage only. So the text the
backend stored and fed onward (the judge prompt, acceptance-criteria search) was the
session's running commentary with the answer somewhere at its end.

Now the ``result`` text is the output whenever the CLI gives one. The live stream is
untouched (the founder still watches the narration), and a CLI or turn that ends without a
``result`` text keeps the joined stream exactly as before.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.executors.worker.executors import ExecutionChunk

_RESULT = {
    "type": "result",
    "subtype": "success",
    "result": "Fixed: the resolver now falls through to the default rule.",
    "usage": {"input_tokens": 1, "output_tokens": 2},
}


def test_the_stream_scrape_keeps_the_final_answer() -> None:
    from backend.executors.worker.claude_code import _StreamScrape, _terminal_chunk

    scrape = _StreamScrape()
    scrape.feed(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me look."}]}}
    )
    scrape.feed(_RESULT)

    assert _terminal_chunk(0, "", scrape).final_text == _RESULT["result"]


def test_a_stream_without_a_result_text_has_no_final_answer() -> None:
    from backend.executors.worker.claude_code import _StreamScrape, _terminal_chunk

    scrape = _StreamScrape()
    scrape.feed({"type": "result", "subtype": "error_max_turns", "usage": {}})

    assert _terminal_chunk(0, "", scrape).final_text is None


async def _drain(chunks: list[ExecutionChunk]) -> dict[str, Any]:
    from backend.executors.worker.main import _result_payload, _stream_and_collect

    class _Executor:
        async def execute(self, prompt: str, context: dict[str, Any]) -> Any:
            for chunk in chunks:
                yield chunk

    outcome = await _stream_and_collect(
        executor=_Executor(), prompt="p", context={}, stream_chan="c", redis=None, task_id="t"
    )
    return _result_payload("t", outcome)


@pytest.mark.asyncio
async def test_the_posted_output_is_the_final_answer() -> None:
    payload = await _drain(
        [
            ExecutionChunk(delta="Let me look at the resolver. "),
            ExecutionChunk(delta="Now running the tests. "),
            ExecutionChunk(delta="Fixed."),
            ExecutionChunk(done=True, final_text="Fixed: falls through to the default rule."),
        ]
    )

    assert payload["output"] == "Fixed: falls through to the default rule."


@pytest.mark.asyncio
async def test_without_a_final_answer_the_stream_is_the_output() -> None:
    """Control — opencode / codex / a turn cut short: exactly the old behaviour."""
    payload = await _drain(
        [ExecutionChunk(delta="a"), ExecutionChunk(delta="b"), ExecutionChunk(done=True)]
    )

    assert payload["output"] == "ab"


@pytest.mark.asyncio
async def test_the_live_stream_still_carries_the_narration() -> None:
    from backend.executors.worker.main import _stream_and_collect

    published: list[dict[str, Any]] = []

    class _Redis:
        async def xadd(self, chan: str, fields: dict[str, Any], **kw: Any) -> None:
            published.append(fields)

        async def publish(self, chan: str, message: Any) -> None:
            published.append({"message": message})

    class _Executor:
        async def execute(self, prompt: str, context: dict[str, Any]) -> Any:
            yield ExecutionChunk(delta="Let me look.")
            yield ExecutionChunk(done=True, final_text="Done.")

    await _stream_and_collect(
        executor=_Executor(), prompt="p", context={}, stream_chan="c", redis=_Redis(), task_id="t"
    )

    assert any("Let me look." in str(p) for p in published)
