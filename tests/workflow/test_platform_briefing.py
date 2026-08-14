"""The platform tells the agent what it gives — every run, unconditionally.

BSVibe has spent a month building a verification surface (a derived repo gate, a
disposable product instance, declared secrets) and never told the agents working
in it that any of that exists. Measured 2026-08-14: ``verify_stack`` → 0 notes,
``client_attach`` → 0 notes, and the whole repo knowledge snapshot frozen at
2026-07-13. The knowledge seed cannot fix this: it retrieves by the run's INTENT
(``knowledge_seed_message`` → ``retrieve_for_signals(_intent_title(run))``), and
"what this platform gives you" is not topically related to any task.

The cost is measurable, not theoretical:

* Run ``010bbdd8`` ran ``mypy <source>`` sixteen times while the derived gate ran
  ``mypy <source> <tests>``. It never knew the gate covers every file it changed.
* The first browser-harness attempt reinvented a CI job and a devDependency. Told
  — in one sentence, with no names — that a disposable per-run environment exists,
  it found the whole apparatus in the repo itself.

So the fix is not a knowledge pipeline. There is already an unconditional slot
(``_SYSTEM_PROMPT``, reaching the executor CLI as ``--append-system-prompt``);
it was simply a month out of date. These tests pin what it must say, and cap what
it may cost — it is paid on every turn of every run.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.workflow.application.agent_briefing import _SYSTEM_PROMPT


def test_the_briefing_says_the_repos_own_checks_decide() -> None:
    """The agent's declared commands are ADVISORY once the derived gate runs
    (``verification_service`` says so explicitly). An agent that believes its own
    green is the verdict re-submits the same failure forever."""
    text = _SYSTEM_PROMPT.lower()
    assert "derive" in text or "derived" in text
    assert "authoritative" in text or "verdict" in text


def test_the_briefing_says_the_gate_covers_every_file_you_changed() -> None:
    """The exact fact run 010bbdd8 lacked: the gate is built from the repo's
    manifests AND the files the step changed, so it checks the tests too."""
    text = _SYSTEM_PROMPT.lower()
    assert "every file you changed" in text or "everything you changed" in text
    assert "test" in text


def test_the_briefing_says_a_disposable_product_instance_exists() -> None:
    """So an agent asked to prove a user-facing change does not invent a CI job
    and a harness dependency — the apparatus is already there."""
    text = _SYSTEM_PROMPT.lower()
    assert "disposable" in text
    # Not a bare "ci" substring — "decision" contains one, so that assertion
    # would pass on the pre-change prompt and prove nothing.
    assert "invent" in text


def test_the_briefing_stays_within_its_per_turn_budget() -> None:
    """This string rides EVERY turn of EVERY run, and this repo deliberately
    respects a local-model generation budget (see ``_DESIGN_SPEC_DIRECTIVE``).
    A capability catalogue does not belong here — only what changes behaviour."""
    assert len(_SYSTEM_PROMPT) <= 3_000, f"briefing is {len(_SYSTEM_PROMPT)} chars"


@pytest.mark.asyncio
async def test_the_briefing_is_what_reaches_the_executor_cli() -> None:
    """The chain is ``messages[0]`` → ``ResolverLoopLlm`` splits it into
    ``system=`` → the dispatched task → the worker's ``--append-system-prompt``.
    If the loop's leading system slot stopped being forwarded, every word above
    would still be in the source and reach nobody — the #752 failure exactly.
    """
    from backend.workflow.application.loop_llm import ResolverLoopLlm

    seen: dict[str, Any] = {}

    class _Adapter:
        async def chat(self, *, system: str, messages: list[dict[str, Any]], tools: Any) -> Any:
            seen["system"] = system
            seen["messages"] = messages

            class _R:
                content = "ok"
                tool_calls: tuple[Any, ...] = ()

            return _R()

    llm = ResolverLoopLlm(adapter=_Adapter())  # type: ignore[arg-type]
    await llm.complete(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "do the thing"},
        ],
        tools=None,
    )

    assert seen["system"] == _SYSTEM_PROMPT
    # And it is not ALSO left in the conversation (the wire shape wants it split off).
    assert all(m["role"] != "system" for m in seen["messages"])
