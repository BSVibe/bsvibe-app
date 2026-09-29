"""An API test's workspace override must publish the workspace like the real one (#959).

``backend.api.deps.get_workspace_id`` sets the request's workspace contextvar
(layer 2) AND the Postgres GUC on the request session (layer 3). ~80 API tests
override it with ``lambda: ws`` — which publishes neither, so every route under
test ran with an EMPTY GUC. Fail-open hid it; fail-closed (#959 ③) makes those
routes read zero rows. :func:`tests._support.publishing_workspace` is the
override that does what the dependency does.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_db_session, get_workspace_id
from backend.data.scoping import current_workspace_id

from ._support import db_engine, publishing_workspace, use_real_pg

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not use_real_pg(), reason="the GUC exists only on Postgres"),
]


def _app(sf: async_sessionmaker[AsyncSession], *, begun: bool = False) -> FastAPI:
    """``begun`` puts a query on the request session BEFORE the workspace is
    resolved — the real shape (auth reads ``memberships`` first), in which the
    transaction's ``after_begin`` already fired with no workspace and only an
    explicit GUC publication covers the rest of it."""
    app = FastAPI()

    async def _session():  # type: ignore[no-untyped-def]
        async with sf() as s:
            if begun:
                await s.execute(text("SELECT 1"))
            yield s

    @app.get("/guc")
    async def guc(
        ws: Annotated[uuid.UUID, Depends(get_workspace_id)],
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, str]:
        value = (
            await session.execute(text("SELECT current_setting('app.current_workspace_id', true)"))
        ).scalar_one()
        scoped = current_workspace_id.get()
        return {"ws": str(ws), "guc": value or "", "layer2": str(scoped) if scoped else ""}

    app.dependency_overrides[get_db_session] = _session
    return app


async def _call(app: FastAPI) -> dict[str, str]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return (await c.get("/guc")).json()


async def test_the_publishing_override_arms_the_guc_on_the_route_session() -> None:
    ws = uuid.uuid4()
    async with db_engine() as (engine, _pg):
        app = _app(async_sessionmaker(engine, expire_on_commit=False))
        app.dependency_overrides[get_workspace_id] = publishing_workspace(ws)
        assert await _call(app) == {"ws": str(ws), "guc": str(ws), "layer2": str(ws)}


async def test_it_arms_the_guc_even_when_the_transaction_already_began() -> None:
    ws = uuid.uuid4()
    async with db_engine() as (engine, _pg):
        app = _app(async_sessionmaker(engine, expire_on_commit=False), begun=True)
        app.dependency_overrides[get_workspace_id] = publishing_workspace(ws)
        assert (await _call(app))["guc"] == str(ws)


async def test_control_a_bare_lambda_override_leaves_the_guc_empty() -> None:
    """Negative control — the shape ~80 tests use today."""
    ws = uuid.uuid4()
    async with db_engine() as (engine, _pg):
        app = _app(async_sessionmaker(engine, expire_on_commit=False))
        app.dependency_overrides[get_workspace_id] = lambda: ws
        assert await _call(app) == {"ws": str(ws), "guc": "", "layer2": ""}
