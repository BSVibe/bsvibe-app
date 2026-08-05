"""#690 — the founder directive must reach the coding agent UNtruncated.

A long directive was silently sliced at 512 chars during prompt assembly
(``_intent_title``), so requirements past that point vanished with no signal
(observed: a 1714-char M1 directive lost its 5th list item → half the scope
built, and lint/test/coverage passed on the truncated half). The coding
agent's first user message must carry the WHOLE directive.

The 512-char cap is legitimate only for the SHORT label uses — the WorkStep
title, the audit ``intent`` field, and the knowledge-retrieval signal — which
keep using ``_intent_title``. The prompt directive gets its own uncapped
accessor ``_intent_directive`` consumed via ``_initial_user_message``.

Pure helpers, tested directly (mirrors ``test_drive_loop_merge_conflict.py``).
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application._drive_loop import _initial_user_message
from backend.workflow.application._loop_context import _intent_directive, _intent_title


class _Run:
    """Minimal ExecutionRun stand-in carrying just ``payload``."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload


def _long_directive(n: int) -> str:
    """Deterministic multi-line directive well past the old 512-char cap."""
    return "\n".join(f"line {i}: implement requirement {i}" for i in range(n))


def test_intent_directive_returns_full_text() -> None:
    text = _long_directive(80)
    assert len(text) > 512  # guards the fixture is actually past the old cap
    run = _Run({"intent_text": text})
    assert _intent_directive(run) == text  # not sliced


def test_intent_directive_reads_text_fallback_and_default() -> None:
    assert _intent_directive(_Run({"text": "hello"})) == "hello"
    assert _intent_directive(_Run(None)) == "Untitled run"
    assert _intent_directive(_Run({})) == "Untitled run"


def test_initial_user_message_carries_full_directive() -> None:
    text = _long_directive(80)
    run = _Run({"intent_text": text})
    msg = _initial_user_message(run)
    assert msg == {"role": "user", "content": text}
    # the last requirement is present — nothing past 512 chars is lost
    assert "requirement 79" in msg["content"]


def test_intent_title_stays_capped_for_short_label_uses() -> None:
    # WorkStep title / audit intent / retrieval signal remain bounded
    run = _Run({"intent_text": _long_directive(80)})
    assert len(_intent_title(run)) == 512
