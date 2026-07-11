"""Unified routing Lift 3 — the external /chat/completions gateway resolves
its ModelAccount through the SAME ``ModelAccountResolver`` the internal
workflow callers use (caller_id ``chat.completions``), instead of demanding
an explicit ``metadata.bsvibe_model_account_id``.
"""

from __future__ import annotations

import uuid

import pytest

from backend.api.v1.chat import _resolve_chat_model_account_id
from backend.config import get_settings
from backend.dispatch.caller_registry import (
    CALLER_CHAT_COMPLETIONS,
    KNOWN_CALLERS,
    get_caller_spec,
)
from backend.dispatch.resolver import NoMatchingRouteError
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.db import RunRoutingRuleRow

from .._support import memory_session


def _exec_account(
    ws: uuid.UUID, litellm_model: str, executor_type: str = "claude_code"
) -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=ws,
        account_id=uuid.uuid4(),
        provider="executor",
        label=litellm_model,
        litellm_model=litellm_model,
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"executor_type": executor_type, "worker_id": str(uuid.uuid4())},
    )


def test_chat_completions_is_a_known_caller() -> None:
    assert CALLER_CHAT_COMPLETIONS == "chat.completions"
    assert CALLER_CHAT_COMPLETIONS in KNOWN_CALLERS
    spec = get_caller_spec(CALLER_CHAT_COMPLETIONS)
    assert "chat" in spec.required_methods


@pytest.mark.asyncio
async def test_explicit_account_id_is_used_verbatim() -> None:
    """An explicit metadata.bsvibe_model_account_id overrides routing — the
    resolver is not consulted (back-compat for callers that pin a model)."""
    explicit = uuid.uuid4()
    async with memory_session() as s:
        got = await _resolve_chat_model_account_id(
            s, get_settings(), workspace_id=uuid.uuid4(), explicit=explicit
        )
    assert got == explicit


@pytest.mark.asyncio
async def test_falls_back_to_workspace_default_when_no_explicit() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        acct = _exec_account(ws, "sonnet")
        s.add(acct)
        s.add(
            WorkspaceRow(
                id=ws,
                name="w",
                region="us-1",
                safe_mode=True,
                legal_basis="x",
                default_account_id=acct.id,
            )
        )
        await s.commit()
        got = await _resolve_chat_model_account_id(
            s, get_settings(), workspace_id=ws, explicit=None
        )
    assert got == acct.id


@pytest.mark.asyncio
async def test_matching_rule_beats_workspace_default() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        default = _exec_account(ws, "sonnet")
        routed = _exec_account(ws, "opus")
        s.add_all([default, routed])
        s.add(
            WorkspaceRow(
                id=ws,
                name="w",
                region="us-1",
                safe_mode=True,
                legal_basis="x",
                default_account_id=default.id,
            )
        )
        s.add(
            RunRoutingRuleRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                name="chat -> opus",
                caller_id=CALLER_CHAT_COMPLETIONS,
                priority=10,
                is_default=False,
                target="opus",
                conditions=[],
                is_active=True,
            )
        )
        await s.commit()
        got = await _resolve_chat_model_account_id(
            s, get_settings(), workspace_id=ws, explicit=None
        )
    assert got == routed.id


@pytest.mark.asyncio
async def test_no_route_no_default_raises() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add(WorkspaceRow(id=ws, name="w", region="us-1", safe_mode=True, legal_basis="x"))
        await s.commit()
        with pytest.raises(NoMatchingRouteError):
            await _resolve_chat_model_account_id(s, get_settings(), workspace_id=ws, explicit=None)
