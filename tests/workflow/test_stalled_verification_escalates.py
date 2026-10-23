"""A verification the agent cannot move is a question for the founder, not another round.

Three ways the loop used to spin on a verdict nobody could change:

* every check passed yet the result was failed — the agent was told "no specific check reported a
  failure" and sent round again;
* the gate was EXPECTED (a manifest exists) but derived zero commands — the one surface that knew
  why was never rendered, so the agent heard nothing at all;
* the same failure came back byte-identical after the agent had already seen it.
"""

from __future__ import annotations

from backend.workflow.application.verify_stall import (
    REPEAT_LIMIT,
    failure_signature,
    stall_question,
)
from backend.workflow.domain.verification_feedback import (
    GATE_DERIVED_EMPTY,
    NO_FAILURE_FOUND,
    render_verification_failure,
)


def test_an_expected_gate_with_zero_commands_says_why() -> None:
    text = render_verification_failure(
        {
            "derived_gate": {
                "origin": "derived",
                "applicable": False,
                "commands": [],
                "passed": True,
            },
            "gate_expected": True,
            "command_results": [{"command": "true", "passed": True, "exit_code": 0}],
        }
    )

    assert text != NO_FAILURE_FOUND
    assert GATE_DERIVED_EMPTY in text


def test_an_empty_gate_on_a_gateless_repo_is_not_a_reason() -> None:
    text = render_verification_failure(
        {
            "derived_gate": {
                "origin": "derived",
                "applicable": False,
                "commands": [],
                "passed": True,
            },
            "gate_expected": False,
        }
    )

    assert text == NO_FAILURE_FOUND


def test_a_round_with_no_failure_found_goes_to_the_founder() -> None:
    question = stall_question(NO_FAILURE_FOUND, repeats=0)

    assert question is not None
    assert "검증" in question["question"]


def test_a_repeated_failure_goes_to_the_founder() -> None:
    failure = "GATE COMMAND FAILED: grep -q x\n  → exit code 1"

    # The first sight, and the second (where the stuck-review hint fires), are the agent's.
    assert stall_question(failure, repeats=0) is None
    assert stall_question(failure, repeats=REPEAT_LIMIT - 1) is None
    repeat = stall_question(failure, repeats=REPEAT_LIMIT)
    assert repeat is not None
    assert "grep -q x" in repeat["context"]


def test_the_same_failure_with_a_changing_duration_is_still_the_same_failure() -> None:
    def result(seconds: str) -> dict:
        return {
            "derived_gate": {
                "commands": [
                    {
                        "command": "pytest -q",
                        "status": "failed",
                        "exit_code": 1,
                        "stdout": f"1 failed in {seconds}s",
                    }
                ]
            }
        }

    assert failure_signature(result("1.14")) == failure_signature(result("2.51"))


def test_a_different_command_or_exit_code_is_a_different_failure() -> None:
    def result(command: str, code: int) -> dict:
        return {
            "derived_gate": {
                "commands": [{"command": command, "status": "failed", "exit_code": code}]
            }
        }

    assert failure_signature(result("pytest", 1)) != failure_signature(result("ruff check", 1))
    assert failure_signature(result("pytest", 1)) != failure_signature(result("pytest", 2))
