"""LLM-derived, repo-grounded verification gate — the pure domain layer.

Replaces the stack-hardcoded quality bar (`uv run ruff`/`mypy` on `.py`) and the
per-stack detector list with ONE mechanism: an LLM reads the repo's OWN
manifests + the changed files and derives the runnable verification commands,
stack-general. This module is the PURE half — the tolerant parser of the LLM's
output + the grounding prompt builder. The LLM call + sandbox execution live in
the service layer (mirrors outcome_demonstration).
"""

from backend.workflow.domain.gate_derivation import (
    DerivedCommand,
    derivation_planner_messages,
    parse_derived_gate,
)


class TestParseDerivedGate:
    def test_parses_commands_with_kind_and_rationale(self) -> None:
        gate = parse_derived_gate(
            {
                "applicable": True,
                "commands": [
                    {
                        "command": "uv run ruff check money.py",
                        "kind": "quality",
                        "rationale": "lint",
                    },
                    {
                        "command": "uv run pytest test_money.py",
                        "kind": "test",
                        "rationale": "suite",
                    },
                ],
            }
        )
        assert gate.applicable is True
        assert gate.commands == (
            DerivedCommand(command="uv run ruff check money.py", kind="quality", rationale="lint"),
            DerivedCommand(command="uv run pytest test_money.py", kind="test", rationale="suite"),
        )
        assert not gate.is_empty

    def test_drops_empty_commands_and_defaults_kind_to_quality(self) -> None:
        gate = parse_derived_gate(
            {"commands": [{"command": ""}, {"cmd": "cargo test"}, {"nope": 1}]}
        )
        # Empty command dropped; `cmd` alias accepted; kind defaults to quality.
        assert gate.commands == (DerivedCommand(command="cargo test", kind="quality"),)

    def test_coerces_unknown_kind_to_quality(self) -> None:
        gate = parse_derived_gate({"commands": [{"command": "go test ./...", "kind": "weird"}]})
        assert gate.commands[0].kind == "quality"

    def test_applicable_false_when_llm_says_non_code(self) -> None:
        # A pure-prose / design deliverable: no runnable gate applies.
        gate = parse_derived_gate({"applicable": False, "commands": []})
        assert gate.applicable is False
        assert gate.is_empty

    def test_applicable_defaults_true_but_empty_commands_stays_empty(self) -> None:
        gate = parse_derived_gate({"commands": []})
        assert gate.applicable is True
        assert gate.is_empty

    def test_tolerates_garbage_shapes(self) -> None:
        for raw in (None, [], "nonsense", 42, {"commands": "notalist"}):
            gate = parse_derived_gate(raw)
            assert gate.is_empty
            # A shape we cannot read at all is not-applicable (honest downgrade),
            # never a spurious runnable gate.
            assert gate.applicable is False

    def test_dedupes_identical_commands(self) -> None:
        gate = parse_derived_gate({"commands": [{"command": "npm test"}, {"command": "npm test"}]})
        assert gate.commands == (DerivedCommand(command="npm test", kind="quality"),)


class TestDerivationPlannerMessages:
    def test_grounds_the_prompt_in_the_repos_real_manifests(self) -> None:
        msgs = derivation_planner_messages(
            manifests={
                "pyproject.toml": "[tool.ruff]\nline-length = 100\n",
                "Makefile": "test:\n\tuv run pytest\n",
            },
            changed_files=["money.py", "test_money.py"],
            intent="Add money utilities",
        )
        assert msgs[0]["role"] == "system"
        joined = "\n".join(m["content"] for m in msgs)
        # The repo's OWN manifest content is in the prompt (grounding) …
        assert "[tool.ruff]" in joined
        assert "Makefile" in joined
        # … along with the changed files it must scope to.
        assert "money.py" in joined
        # The system prompt forbids inventing tools/flags/extras the repo doesn't define.
        sys_lower = msgs[0]["content"].lower()
        assert "invent" in sys_lower or "only" in sys_lower

    def test_no_manifests_still_produces_a_valid_message_pair(self) -> None:
        msgs = derivation_planner_messages(
            manifests={}, changed_files=[], intent="Write a design doc"
        )
        assert [m["role"] for m in msgs] == ["system", "user"]
