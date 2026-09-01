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

from backend.workflow.application.agent_briefing import _ASK_SYSTEM_PROMPT, _SYSTEM_PROMPT


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


# ── 회고 선언 — 요청이 "답할 수 있는 순간"에 존재해야 한다 ────────────────────
#
# 실측 2026-09-01: 회고(``declared_knowledge``)의 유일한 생산 경로는
# ``declare_verification`` 의 OPTIONAL ``knowledge`` 인자다. 그런데 그 툴은 브리핑이
# **"BEFORE any file_write"** 라고 못박아 부르게 하는 툴이다 — 즉 에이전트가 그 블록을
# 채울 수 있는 유일한 순간(작업을 마친 뒤)에는 아무도 그것을 요청하지 않는다.
# 스키마 자신이 *"Only you, who did the work, can see the tacit knowledge"* 라고 쓰면서
# 정작 **작업 전에** 묻는다.
#
# 브리핑 전문을 검색해도 ``knowledge`` 는 한 글자도 없었다. 선언율 9.4%(코드 변경 96건
# 중 9건)는 프롬프트가 서툴러서가 아니라 **요청이 존재하지 않아서**다.
#
# 없던 것은 서브시스템이 아니라 링크 하나다: 끝에서 한 번 물어보는 문장.


def test_the_briefing_asks_for_the_retrospection_at_the_end() -> None:
    """끝나는 순간에 회고를 요청한다 — 그 블록을 채울 수 있는 유일한 시점.

    ⚠️ 이 단언들은 철자 목록이 아니라 **명제**다: (1) 회고를 담는 인자 이름을 대고,
    (2) 그것을 나르는 툴을 다시 지목하며, (3) 시점이 '끝'임을 말한다. 셋 중 하나라도
    빠지면 에이전트는 무엇을·어디에·언제 써야 하는지 알 수 없다.
    """
    text = _SYSTEM_PROMPT.lower()
    # (1) 인자 이름 — 이게 없으면 "회고를 남겨라"는 실행 불가능한 격려문이다.
    assert "knowledge" in text
    # (2) 나르는 툴 — 회고는 자기 툴이 없다(``record_knowledge`` 는 주석에만 있었다).
    #     ``declare_verification`` 을 다시 부르는 것이 유일한 경로다.
    assert "declare_verification" in text
    # (3) 시점 — 처음이 아니라 끝. 처음엔 배운 것이 없다.
    assert "again" in text or "once more" in text


def test_the_retrospection_ask_stays_out_of_the_investigation_prompt() -> None:
    """ASK 런의 정체성에는 쓰기 워크플로를 들이지 않는다.

    prod ``fae09a47`` 실측 — ``_ASK_SYSTEM_PROMPT`` 가 ``declare_verification`` 을
    **이름만 대도** 쓰기를 부른다("naming the write workflow at all invites the write").
    회고를 늘리자고 그 판정을 되돌리면 조사 런이 다시 파일을 고친다.
    """
    # 명제는 하나다: **회고를 나르는 채널을 이름 대지 않는다.** 채널이 없으면 요청도 없다.
    #
    # ⚠️ 여기서 낱말을 더 세려던 두 번의 시도가 둘 다 틀렸다 — ``file_write`` 는 ASK
    # 프롬프트가 **금지문으로** 이름 대고("do not use file_write or file_edit"),
    # ``knowledge`` 는 ``knowledge_search`` 에 들어 있다. 철자를 세면 명제가 아니라
    # 내 상상력을 검사하게 된다.
    assert "declare_verification" not in _ASK_SYSTEM_PROMPT


def test_the_briefing_warns_that_redeclaring_replaces_the_contract() -> None:
    """회고를 남기라고 시키는 이상, 그 대가도 같이 말해야 한다.

    실측(``test_redeclaring_replaces_the_contract_so_the_briefing_must_say_so``):
    두 번째 ``declare_verification`` 은 앞선 checks 를 **덮어쓴다**. 그 사실을 빼고
    "한 번 더 불러라"만 말하면, 회고를 남기려던 에이전트가 stub check 로 재선언해
    자기 계약을 파괴한다. 초안이 정확히 그렇게 틀려 있었다.
    """
    text = _SYSTEM_PROMPT.lower()
    assert "repeating your same checks" in text
    assert "replaces" in text
