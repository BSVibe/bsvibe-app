"""Lift A — Router facade Protocol shape tests.

These tests assert the Router Protocol *exists with the right shape*. No real
behavior — concrete implementations come in later lifts (B/C/D).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, get_type_hints

import pytest

from backend.router.facade import (
    LlmRequest,
    LlmResult,
    LlmRoutingHints,
    Router,
)


def test_router_is_runtime_checkable() -> None:
    """Router Protocol must be decorated with @runtime_checkable."""
    # _is_runtime_protocol is the marker typing sets on @runtime_checkable Protocols.
    assert getattr(Router, "_is_runtime_protocol", False) is True


def test_minimal_mock_conforms_to_router() -> None:
    """A minimal object with an async ``invoke`` matching the signature passes isinstance."""

    class _Mock:
        async def invoke(self, request: LlmRequest) -> LlmResult:  # noqa: ARG002
            return LlmResult(
                content="",
                usage_prompt_tokens=0,
                usage_completion_tokens=0,
            )

    mock = _Mock()
    assert isinstance(mock, Router)


def test_llm_routing_hints_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(LlmRoutingHints)
    assert LlmRoutingHints.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    hints = LlmRoutingHints()
    assert hints.pipeline is None
    assert hints.workspace_id is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        hints.pipeline = "x"  # type: ignore[misc]


def test_llm_routing_hints_field_types() -> None:
    hints_types = get_type_hints(LlmRoutingHints)
    assert hints_types["pipeline"] == (str | None)
    assert hints_types["workspace_id"] == (uuid.UUID | None)


def test_llm_request_is_frozen_dataclass_with_expected_fields() -> None:
    assert dataclasses.is_dataclass(LlmRequest)
    assert LlmRequest.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    field_names = {f.name for f in dataclasses.fields(LlmRequest)}
    assert field_names == {"workspace_id", "messages", "tools", "hints"}


def test_llm_request_field_types() -> None:
    hints_types = get_type_hints(LlmRequest)
    assert hints_types["workspace_id"] is uuid.UUID
    assert hints_types["messages"] == list[dict[str, Any]]
    assert hints_types["tools"] == (list[dict[str, Any]] | None)
    assert hints_types["hints"] is LlmRoutingHints


def test_llm_request_hints_default_factory() -> None:
    request = LlmRequest(workspace_id=uuid.uuid4(), messages=[])
    assert isinstance(request.hints, LlmRoutingHints)
    assert request.tools is None


def test_llm_result_is_frozen_dataclass_with_expected_fields() -> None:
    assert dataclasses.is_dataclass(LlmResult)
    assert LlmResult.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    field_names = {f.name for f in dataclasses.fields(LlmResult)}
    assert field_names == {
        "content",
        "usage_prompt_tokens",
        "usage_completion_tokens",
        "tool_calls",
        "resolved_model_label",
    }


def test_llm_result_field_types() -> None:
    hints_types = get_type_hints(LlmResult)
    assert hints_types["content"] is str
    assert hints_types["usage_prompt_tokens"] is int
    assert hints_types["usage_completion_tokens"] is int
    assert hints_types["tool_calls"] == tuple[dict[str, Any], ...]
    assert hints_types["resolved_model_label"] is str


def test_llm_result_defaults() -> None:
    result = LlmResult(content="hi", usage_prompt_tokens=1, usage_completion_tokens=2)
    assert result.tool_calls == ()
    assert result.resolved_model_label == ""
