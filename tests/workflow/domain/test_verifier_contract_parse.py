"""Lift E38 — parser tolerance for the verification-contract shapes the
agent actually emits.

The E37 dogfood (session ``ses_12c8f0be2``, 2026-06-17) proved the
restructured prompt makes qwen3.6-plus emit the ``<verification-contract>``
block reliably — but the agent emits ``{"kind": "shell", "command": "…"}``
(matching what the prose calls a "shell command") while the parser only
accepts ``{"kind": "command", "command": "…"}``. The agent kept declaring,
the parser kept rejecting with "no usable check", the loop kept re-prompting,
and the run sat in a 40-minute spin without ever landing terminal.

E38 fixes the gap on both sides:

* the agent guide template now uses ``kind: "command"`` (matches the
  parser's enum verbatim, see :mod:`backend.dispatch.adapter`);
* the parser ALSO accepts ``kind: "shell"`` as an alias and ``cmd`` as
  an alias for ``command`` — the natural-English forms a model is most
  likely to produce when the prompt template is paraphrased.
"""

from __future__ import annotations

from backend.workflow.domain.verifier_contract import parse_verification_contract


def test_parse_accepts_kind_shell_as_alias_for_command() -> None:
    """qwen3.6-plus picked ``kind: "shell"`` from the E37 prompt template;
    the parser now accepts it the same way as ``kind: "command"``."""
    contract = parse_verification_contract(
        {"checks": [{"kind": "shell", "command": "test -f marker"}]}
    )
    assert contract is not None
    assert len(contract.command_checks) == 1
    assert contract.command_checks[0].command == "test -f marker"


def test_parse_accepts_cmd_as_alias_for_command_field() -> None:
    """The E37 template originally said ``cmd`` (now corrected to
    ``command``). Parser still accepts ``cmd`` for any past-template
    cache or future paraphrase by the agent."""
    contract = parse_verification_contract(
        {"checks": [{"kind": "command", "cmd": "test -f marker"}]}
    )
    assert contract is not None
    assert len(contract.command_checks) == 1
    assert contract.command_checks[0].command == "test -f marker"


def test_parse_accepts_both_shell_alias_and_cmd_alias_together() -> None:
    """The realistic shape the E37 dogfood agent emitted."""
    contract = parse_verification_contract(
        {"checks": [{"kind": "shell", "cmd": "grep -q docstring file.py"}]}
    )
    assert contract is not None
    assert contract.command_checks[0].command == "grep -q docstring file.py"


def test_parse_canonical_command_still_works() -> None:
    """Regression: the canonical ``{"kind": "command", "command": ...}``
    shape used by every existing test continues to parse unchanged."""
    contract = parse_verification_contract({"checks": [{"kind": "command", "command": "pytest"}]})
    assert contract is not None
    assert contract.command_checks[0].command == "pytest"


def test_parse_rejects_empty_command_in_shell_kind() -> None:
    """An empty command under either alias is still unusable."""
    assert parse_verification_contract({"checks": [{"kind": "shell", "command": ""}]}) is None
    assert parse_verification_contract({"checks": [{"kind": "shell", "cmd": ""}]}) is None


def test_parse_rejects_unknown_kind() -> None:
    """``kind: "lint"`` (or any other word) still fails the kind check."""
    assert (
        parse_verification_contract({"checks": [{"kind": "lint", "command": "ruff check"}]}) is None
    )
