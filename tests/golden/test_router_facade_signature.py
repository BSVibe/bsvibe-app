"""Golden test — pin the Router facade Protocol signature (Lift N-Coverage #6).

The Router facade is the public seam between every dispatch path and the
LLM substrate (v8 §5.1). Any change to its public methods, parameter
names, or annotations must be deliberate — this test pins the exact
shape so an accidental drift fails CI loudly.

Source: ``backend/router/facade.py`` (Lift A).
"""

from __future__ import annotations

import inspect
import uuid
from typing import Protocol, get_type_hints

from backend.router.facade import (
    LlmRequest,
    LlmResult,
    LlmRoutingHints,
    Router,
)

# --- Protocol shape -----------------------------------------------------

EXPECTED_PUBLIC_METHODS: frozenset[str] = frozenset({"invoke"})


def test_router_protocol_public_methods_are_exactly_pinned() -> None:
    """The Router Protocol exposes EXACTLY the pinned public method set.

    Adding or removing a public method on the Router facade is a v8 §5.1
    contract change and requires the design doc to be updated first.
    """
    actual = frozenset(
        name
        for name in dir(Router)
        if not name.startswith("_") and callable(getattr(Router, name, None))
    )
    assert actual == EXPECTED_PUBLIC_METHODS, (
        "Router facade public method set drift detected.\n"
        f"  expected: {sorted(EXPECTED_PUBLIC_METHODS)}\n"
        f"  actual:   {sorted(actual)}\n"
        "If this is intentional, update v8 §5.1 + this golden test."
    )


def test_router_invoke_signature_is_pinned() -> None:
    """``Router.invoke`` MUST be ``(self, request: LlmRequest) -> LlmResult``."""
    sig = inspect.signature(Router.invoke)
    params = list(sig.parameters.values())

    # self + request — no more, no less.
    assert [p.name for p in params] == ["self", "request"], (
        f"Router.invoke parameter list drift: {[p.name for p in params]}"
    )

    hints = get_type_hints(Router.invoke)
    assert hints.get("request") is LlmRequest, (
        f"Router.invoke request annotation drift: {hints.get('request')!r}"
    )
    assert hints.get("return") is LlmResult, (
        f"Router.invoke return annotation drift: {hints.get('return')!r}"
    )


def test_router_is_runtime_checkable_protocol() -> None:
    """Router must remain a ``@runtime_checkable`` ``Protocol`` (v8 §5.1)."""
    assert issubclass(Router, Protocol), "Router must subclass Protocol"
    # runtime_checkable Protocols expose ``_is_runtime_protocol`` = True.
    assert getattr(Router, "_is_runtime_protocol", False), (
        "Router must be @runtime_checkable so isinstance() works at the seam."
    )


# --- Dataclass shapes ---------------------------------------------------


def test_llm_routing_hints_field_shape_is_pinned() -> None:
    hints = get_type_hints(LlmRoutingHints)
    # Field names + their type annotations.
    assert set(hints.keys()) == {"pipeline", "workspace_id"}, (
        f"LlmRoutingHints field set drift: {sorted(hints.keys())}"
    )
    assert hints["pipeline"] == (str | None)
    assert hints["workspace_id"] == (uuid.UUID | None)


def test_llm_request_field_shape_is_pinned() -> None:
    hints = get_type_hints(LlmRequest)
    assert set(hints.keys()) == {
        "workspace_id",
        "messages",
        "tools",
        "hints",
    }, f"LlmRequest field set drift: {sorted(hints.keys())}"
    assert hints["workspace_id"] is uuid.UUID
    assert hints["hints"] is LlmRoutingHints


def test_llm_result_field_shape_is_pinned() -> None:
    hints = get_type_hints(LlmResult)
    assert set(hints.keys()) == {
        "content",
        "usage_prompt_tokens",
        "usage_completion_tokens",
        "tool_calls",
        "resolved_model_label",
    }, f"LlmResult field set drift: {sorted(hints.keys())}"
    assert hints["content"] is str
    assert hints["usage_prompt_tokens"] is int
    assert hints["usage_completion_tokens"] is int
    assert hints["resolved_model_label"] is str


# --- Structural runtime check ------------------------------------------


def test_minimal_router_passes_structural_runtime_check() -> None:
    """A minimal duck-typed concrete must satisfy ``isinstance(_, Router)``.

    This guards the structural-typing contract: callers can swap in any
    object exposing the pinned method, and isinstance() at the seam will
    accept it.
    """

    class _MinimalRouter:
        async def invoke(self, request: LlmRequest) -> LlmResult:  # noqa: ARG002
            return LlmResult(
                content="",
                usage_prompt_tokens=0,
                usage_completion_tokens=0,
            )

    assert isinstance(_MinimalRouter(), Router)
