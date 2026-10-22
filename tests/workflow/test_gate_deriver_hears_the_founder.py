"""The derived gate is re-derived from the request on EVERY verification — so the founder's
answers have to be part of that derivation, or they bind only the agent.

Run ``f7c5b4d1`` (PR #1207): the request's own sentence implied a ``grep -q 'docker exec -w …'``
gate. The agent asked, the founder answered "keep `--context colima`, relax the gate", and the
next verification re-derived the very same grep from the request. The answer reached the agent and
never the deriver, so obeying the founder failed the gate and obeying the gate defied the founder.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.gate_derivation import (
    derivation_planner_messages,
    founder_decisions_from_payload,
)

_REJECTED = "docker exec -w /app bsvibe-prod-backend-1"


def _user(messages: list[dict[str, str]]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user")


def _system(messages: list[dict[str, str]]) -> str:
    return next(m["content"] for m in messages if m["role"] == "system")


def test_the_founders_answer_is_in_the_derivers_input() -> None:
    messages = derivation_planner_messages(
        manifests={"pyproject.toml": "[project]"},
        changed_files=["docs/runbook.md"],
        intent=f"document `{_REJECTED} …`",
        founder_decisions=[
            {
                "question": "The gate wants `docker exec`, the rule wants --context colima.",
                "answer": "Keep --context colima in the doc; relax the gate.",
            }
        ],
    )

    user = _user(messages)
    assert "Keep --context colima in the doc; relax the gate." in user
    assert "gate wants `docker exec`" in user


def test_the_answer_outranks_the_requests_own_sentence() -> None:
    messages = derivation_planner_messages(
        manifests={}, changed_files=[], intent="x", founder_decisions=[{"answer": "Y not X"}]
    )

    system = _system(messages)
    assert "FOUNDER DECISIONS" in system
    assert "never" in system.lower()  # a rejected command is not emitted again


def test_no_decisions_means_no_block() -> None:
    messages = derivation_planner_messages(manifests={}, changed_files=[], intent="x")

    assert "Founder decisions" not in _user(messages)


def test_decisions_come_out_of_the_run_payload_including_a_denial_reason() -> None:
    payload: dict[str, Any] = {
        "resolved_decisions": [
            {"decision_id": "1", "question": "Q1", "answer": "A1"},
            {
                "decision_id": "2",
                "question": "Approve delivering this run's result?",
                "answer": "Denied. do not grep the docs",
            },
            {"decision_id": "3", "question": "Q3", "answer": ""},
            "junk",
        ]
    }

    assert founder_decisions_from_payload(payload) == [
        {"question": "Q1", "answer": "A1"},
        {
            "question": "Approve delivering this run's result?",
            "answer": "Denied. do not grep the docs",
        },
    ]
    assert founder_decisions_from_payload({}) == []
    assert founder_decisions_from_payload(None) == []


async def test_the_service_hands_the_runs_decisions_to_the_deriver() -> None:
    seen: list[list[dict[str, str]]] = []

    async def complete(_run: Any, messages: list[dict[str, str]]) -> Any:
        seen.append(messages)
        return SimpleNamespace(content='{"applicable": false, "commands": []}')

    service = VerificationService.__new__(VerificationService)
    service._complete = complete  # type: ignore[method-assign]
    run = SimpleNamespace(
        payload={"resolved_decisions": [{"question": "Q", "answer": "relax the gate"}]}
    )

    await service._author_derived_gate(run, "intent", {"pyproject.toml": "x"}, [])  # type: ignore[arg-type]

    assert "relax the gate" in _user(seen[0])
