"""L10 (#4/#5) — a Direct *question* is answered inline, never sent to the loop.

The prod symptom: a question dispatched as a run hit the executor and crashed
("executor chat task … failed: exit 1"). The inline ``POST /api/v1/messages/ask``
classifies question-vs-work and, for a question, returns a synchronous chat
answer (no run, no executor). A work request → ``answered=False`` so the PWA
falls back to the normal async dispatch.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.api.v1.messages as messages_api
from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.workflow.application.direct_answer import is_question

from .._support import db_engine, fake_current_user


@pytest.mark.parametrize(
    "text",
    ["지금 프로젝트 상황 어때?", "how's the project doing?", "what is our deploy process?"],
)
def test_is_question_true(text) -> None:
    assert is_question(text) is True


@pytest.mark.parametrize(
    "text",
    ["add a mean() utility to the backend", "build a TTL cache", "fix the broken link"],
)
def test_is_question_false(text) -> None:
    assert is_question(text) is False


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(sf):
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: uuid.uuid4()

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_ask_work_request_not_answered(client) -> None:
    """A build request is NOT a question → answered=False (PWA dispatches it)."""
    resp = await client.post("/api/v1/messages/ask", json={"text": "build a TTL cache module"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answered"] is False


async def test_ask_question_returns_inline_answer(client, monkeypatch) -> None:
    """A question is answered inline (chat model stubbed)."""

    class _StubService:
        def __init__(self, session, *, settings, redis=None) -> None:  # noqa: ANN001
            pass

        async def answer(self, *, workspace_id, text):  # noqa: ANN001, ANN201
            return "The project shipped 9 lifts this round."

    monkeypatch.setattr(messages_api, "DirectAnswerService", _StubService)
    resp = await client.post("/api/v1/messages/ask", json={"text": "지금 프로젝트 상황 어때?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answered"] is True
    assert "9 lifts" in body["answer"]


async def test_ask_question_no_chat_model_falls_back(client, monkeypatch) -> None:
    """A question but no chat model resolves → answered=False (dispatch as work)."""

    class _NoneService:
        def __init__(self, session, *, settings, redis=None) -> None:  # noqa: ANN001
            pass

        async def answer(self, *, workspace_id, text):  # noqa: ANN001, ANN201
            return None

    monkeypatch.setattr(messages_api, "DirectAnswerService", _NoneService)
    resp = await client.post("/api/v1/messages/ask", json={"text": "how's the project doing?"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answered"] is False
