"""The token a dispatched executor task carries (T2).

The executor is the user's LLM client: to act on a run it calls BSVibe's tools over MCP, and
for that it needs a credential. This is the narrowest one that works — and narrow matters,
because it lives on the founder's own machine, inside a CLI subprocess, for the duration of a
task.

* **One run.** The ``run_id`` claim binds it to a single ExecutionRun's worktree. A leak
  reaches that run; it cannot read another, and it cannot touch the workspace at large.
* **Short.** Minutes, not the founder's session. It expires with the task.
* **No refresh.** :func:`issue_token_pair` mints a refresh token — a long-lived secret that
  can re-mint access. A task never needs one; handing it one would turn a task credential into
  a durable foothold.
* **Revocable.** It gets the same ``OAuthAccessTokenRow`` every other token has, so the
  existing revocation + expiry checks apply unchanged.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.identity.oauth_db import OAuthAccessTokenRow, OAuthRefreshTokenRow
from backend.identity.oauth_jwt import verify_access_token
from backend.identity.oauth_service import issue_run_task_token

from .._support import memory_session

pytestmark = pytest.mark.asyncio

_ISSUER = "https://api.bsvibe.dev"


async def test_the_token_names_the_run() -> None:
    run_id, ws, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async with memory_session() as session:
        token = await issue_run_task_token(
            session, run_id=run_id, workspace_id=ws, user_id=user, issuer=_ISSUER
        )
        await session.commit()

    claims = verify_access_token(token, issuer=_ISSUER)
    assert claims["run_id"] == str(run_id)
    assert claims["wsp"] == str(ws)


async def test_no_refresh_token_is_minted() -> None:
    """A task credential must not be able to re-mint itself into a durable foothold."""
    async with memory_session() as session:
        await issue_run_task_token(
            session,
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            issuer=_ISSUER,
        )
        await session.commit()

        rows = (await session.execute(select(OAuthRefreshTokenRow))).scalars().all()

    assert rows == []


async def test_it_is_revocable_like_every_other_token() -> None:
    """It rides the same access-token row, so revocation and expiry work unchanged."""
    ws = uuid.uuid4()

    async with memory_session() as session:
        await issue_run_task_token(
            session, run_id=uuid.uuid4(), workspace_id=ws, user_id=uuid.uuid4(), issuer=_ISSUER
        )
        await session.commit()

        row = (await session.execute(select(OAuthAccessTokenRow))).scalars().one()

    assert row.workspace_id == ws
    assert row.revoked_at is None


async def test_it_expires_within_minutes_not_a_session() -> None:
    """Long enough for a coding task, short enough that a leaked token dies on its own."""
    async with memory_session() as session:
        await issue_run_task_token(
            session,
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            issuer=_ISSUER,
        )
        await session.commit()

        row = (await session.execute(select(OAuthAccessTokenRow))).scalars().one()

    lifetime = row.expires_at - row.issued_at
    assert timedelta(minutes=5) <= lifetime <= timedelta(hours=2)


async def test_the_scope_is_only_what_the_work_tools_need() -> None:
    async with memory_session() as session:
        await issue_run_task_token(
            session,
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            issuer=_ISSUER,
        )
        await session.commit()

        row = (await session.execute(select(OAuthAccessTokenRow))).scalars().one()

    assert set(row.scope) == {"mcp:read", "mcp:write"}
