"""resolve_notify_bindings / available_channels — channels derived from connectors.

The notification channel model is DERIVED, not hardcoded: a workspace's channels
are ``in_app`` plus every active connector binding that (a) has a non-empty
``delivery_config``, (b) is a notify channel (in ``NOTIFY_EVENT_BUILDERS``), and
(c) is ``user_connectable`` (not hidden). These tests exercise each condition
against a real in-memory session seeded with connector rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Registers ``connector_accounts`` on the shared Base.metadata.
import backend.connectors.db  # noqa: F401
from backend.connectors.db import ConnectorAccountRow
from backend.notifications import bindings as bindings_mod
from backend.notifications.bindings import available_channels, resolve_notify_bindings

from .._support import memory_session

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    async with memory_session() as s:
        yield s


def _account(
    workspace_id: uuid.UUID,
    connector: str,
    *,
    delivery_config: dict | None = None,
    is_active: bool = True,
) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        workspace_id=workspace_id,
        connector=connector,
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config={"chat_id": "123"} if delivery_config is None else delivery_config,
        is_active=is_active,
    )


async def test_only_notify_connectors_qualify(session) -> None:
    ws = uuid.uuid4()
    # telegram is a notify channel; notion + sentry deliver work out but are not
    # notify channels (no NOTIFY_EVENT_BUILDER) → deliberate seam.
    session.add_all(
        [
            _account(ws, "telegram"),
            _account(ws, "notion", delivery_config={"parent_page_id": "p"}),
            _account(ws, "sentry", delivery_config={"issue_id": "i"}),
        ]
    )
    await session.commit()

    result = await resolve_notify_bindings(session, workspace_id=ws)
    assert [b.connector for b in result] == ["telegram"]


async def test_empty_delivery_config_is_skipped(session) -> None:
    ws = uuid.uuid4()
    session.add(_account(ws, "telegram", delivery_config={}))
    await session.commit()
    assert await resolve_notify_bindings(session, workspace_id=ws) == []


async def test_inactive_binding_is_skipped(session) -> None:
    ws = uuid.uuid4()
    session.add(_account(ws, "slack", is_active=False))
    await session.commit()
    assert await resolve_notify_bindings(session, workspace_id=ws) == []


async def test_other_workspace_bindings_are_not_leaked(session) -> None:
    ws, other = uuid.uuid4(), uuid.uuid4()
    session.add_all([_account(ws, "telegram"), _account(other, "slack")])
    await session.commit()
    result = await resolve_notify_bindings(session, workspace_id=ws)
    assert [b.connector for b in result] == ["telegram"]


async def test_hidden_connector_excluded_via_user_connectable(session, monkeypatch) -> None:
    """A notify-capable but HIDDEN connector is excluded (user_connectable=False).

    No shipped connector is both notify-capable and hidden today, so simulate it
    by hiding ``telegram`` — the guard must drop it even though it has a builder.
    """
    monkeypatch.setattr(bindings_mod, "HIDDEN_CONNECTORS", frozenset({"telegram"}), raising=True)
    ws = uuid.uuid4()
    session.add_all([_account(ws, "telegram"), _account(ws, "slack")])
    await session.commit()
    result = await resolve_notify_bindings(session, workspace_id=ws)
    assert [b.connector for b in result] == ["slack"]


async def test_available_channels_prepends_in_app(session) -> None:
    ws = uuid.uuid4()
    session.add(_account(ws, "telegram"))
    await session.commit()
    assert await available_channels(session, workspace_id=ws) == ["in_app", "telegram"]


async def test_available_channels_is_in_app_only_with_no_connectors(session) -> None:
    ws = uuid.uuid4()
    assert await available_channels(session, workspace_id=ws) == ["in_app"]


async def test_available_channels_sorted_and_deduped(session) -> None:
    ws = uuid.uuid4()
    session.add_all([_account(ws, "telegram"), _account(ws, "slack"), _account(ws, "slack")])
    await session.commit()
    # in_app first, connectors sorted, duplicate slack collapsed.
    assert await available_channels(session, workspace_id=ws) == ["in_app", "slack", "telegram"]
