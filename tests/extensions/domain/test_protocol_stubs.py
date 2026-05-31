"""Lift G — extension Protocol stubs are runtime_checkable + zero registered impl.

Per the Lift G plan (no live wiring this lift):

* ``ActionDispatchInterceptor`` — pre-action gate, no registered impl.
* ``SettlementSubscriber`` — settlement / rollback hook, no registered impl.
* ``EventBus`` + ``EventBusSubscriber`` — pub/sub Protocols, audit is the
  first *concrete* user but registers no subscriber in this lift.
* ``Plugin`` / ``Skill`` / ``Action`` — formalize what plugin/skill loaders
  already produce.

We assert each Protocol is ``runtime_checkable`` and that the codebase has
*zero* live ``register_*`` call sites for the new hook Protocols — the
contract is published-but-unused this lift.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, get_type_hints

import pytest

from backend.extensions.domain import protocols


@pytest.mark.parametrize(
    "name",
    [
        "ActionDispatchInterceptor",
        "SettlementSubscriber",
        "EventBus",
        "EventBusSubscriber",
        "Plugin",
        "Skill",
        "Action",
    ],
)
def test_protocol_is_runtime_checkable(name: str) -> None:
    proto = getattr(protocols, name)
    assert issubclass(proto, Protocol), f"{name} must be a Protocol subclass"
    # runtime_checkable Protocols expose _is_runtime_protocol = True
    assert getattr(proto, "_is_runtime_protocol", False), (
        f"{name} must be marked @runtime_checkable"
    )


@pytest.mark.parametrize(
    "name",
    ["ActionDispatchInterceptor", "SettlementSubscriber", "EventBusSubscriber"],
)
def test_hook_protocol_has_zero_registered_impl(name: str) -> None:
    """Lift G publishes hook surfaces but does not wire any concrete impl.

    Grep the entire backend tree for ``register_<hook>`` — must return 0
    hits. (Test files referencing the name are allowed; production code is
    not. We scope the grep to ``backend/``.)
    """
    repo_root = Path(__file__).resolve().parents[3]
    backend = repo_root / "backend"
    # snake_case the hook name for register_<snake>(...) lookups.
    snake = "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")
    pattern = f"register_{snake}"
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-l", pattern, str(backend)],
        capture_output=True,
        text=True,
        check=False,
    )
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    assert hits == [], f"Lift G expects zero live registrations of {pattern}; found: {hits}"


def test_action_dispatch_interceptor_signature() -> None:
    """The interceptor protocol must take a context-ish payload and return
    a decision (allow / deny). Keep the surface tiny — Lift G is publication
    only."""
    hints = get_type_hints(protocols.ActionDispatchInterceptor.before_dispatch)
    # Argument names: self, context — we just assert the method exists.
    assert callable(protocols.ActionDispatchInterceptor.before_dispatch)
    assert "return" in hints
