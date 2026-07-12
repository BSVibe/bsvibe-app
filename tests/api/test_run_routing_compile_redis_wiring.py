"""REGRESSION — the API-layer resolvers must be wired with the app's Redis client.

The NL routing compiler (``POST /api/v1/run-routing/compile``, ``/compile/apply``
and the N5 ``source_text`` compile on rule create/update) never worked in
production. The compile LLM resolves to the workspace's default ModelAccount,
which for the founder is an **executor** account (provider=``executor``);
:meth:`backend.dispatch.adapter.ExecutorAdapter.chat` dispatches onto a Redis
worker stream and raises :class:`ExecutorAdapterUnavailable` without a Redis
client. The API built its :class:`ModelAccountResolver` with NO ``redis=`` — the
workflow runtime threads one, the API layer did not — so EVERY compile blew up:

    event: routing_source_text_compile_llm_failed
    ExecutorAdapterUnavailable: ExecutorAdapter requires a Redis client to
    dispatch onto the worker stream — no redis was wired into the resolver

Every existing test mocked the LLM seam, so nothing exercised the WIRING. These
tests assert the wiring itself: the redis client the app owns is threaded into
the resolver on both API compile paths (run-routing + the external chat gateway),
and ``create_app`` publishes it where those handlers can reach it. They fail on
the pre-fix code (``redis`` arrives as ``None``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.identity.workspaces_db  # noqa: F401 — register workspaces table
import backend.router.accounts.account_models  # noqa: F401 — register accounts table
import backend.router.accounts.models  # noqa: F401 — register model_accounts table
import backend.router.routing.run_routing.db  # noqa: F401 — register run_routing_rules table
from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
    require_account_id,
)
from backend.api.main import create_app
from backend.api.redis_client import get_api_redis, set_api_redis
from backend.dispatch.resolver import ModelAccountResolver

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


class _SentinelRedis:
    """Stand-in for the app's ``redis.asyncio.Redis`` — identity is all we assert."""


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_api_redis() -> Iterator[None]:
    """The accessor is a process-wide singleton — never leak across tests."""
    set_api_redis(None)
    yield
    set_api_redis(None)


@pytest_asyncio.fixture
async def client(maker, workspace_id) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    account_id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_account_id] = lambda: account_id
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def resolver_spy(monkeypatch) -> list[dict[str, Any]]:
    """Record the kwargs every :class:`ModelAccountResolver` is constructed with."""
    seen: list[dict[str, Any]] = []
    original = ModelAccountResolver.__init__

    def _spy(self: ModelAccountResolver, session: Any, **kwargs: Any) -> None:
        seen.append(kwargs)
        original(self, session, **kwargs)

    monkeypatch.setattr(ModelAccountResolver, "__init__", _spy)
    return seen


# ---------------------------------------------------------------------------
# The bug, at each of the three API compile entry points.
#
# No routing rule + no workspace default → the resolver raises
# NoMatchingRouteError → the endpoint 400s. That terminal is incidental: the
# assertion is that the resolver was CONSTRUCTED with the app's redis client.
# ---------------------------------------------------------------------------
async def test_compile_endpoint_threads_api_redis_into_the_resolver(client, resolver_spy) -> None:
    redis = _SentinelRedis()
    set_api_redis(redis)

    await client.post("/api/v1/run-routing/compile", json={"text": "복잡한 건 opus"})

    assert resolver_spy, "the compile path never constructed a ModelAccountResolver"
    assert resolver_spy[-1].get("redis") is redis, (
        "the compile resolver was built WITHOUT the app's redis client — an "
        "executor-backed compile model raises ExecutorAdapterUnavailable"
    )


async def test_source_text_create_threads_api_redis_into_the_resolver(client, resolver_spy) -> None:
    redis = _SentinelRedis()
    set_api_redis(redis)

    await client.post(
        "/api/v1/run-routing",
        json={"name": "복잡한 작업", "source_text": "복잡한 작업", "target": "opus"},
    )

    assert resolver_spy, "the source_text compile never constructed a ModelAccountResolver"
    assert resolver_spy[-1].get("redis") is redis


async def test_chat_completions_threads_api_redis_into_the_resolver(client, resolver_spy) -> None:
    """The external gateway carries the SAME latent bug — an executor-backed
    default account resolves here too."""
    redis = _SentinelRedis()
    set_api_redis(redis)

    await client.post(
        "/api/v1/chat/completions",
        json={"model": "opus", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resolver_spy, "the chat path never constructed a ModelAccountResolver"
    assert resolver_spy[-1].get("redis") is redis


# ---------------------------------------------------------------------------
# No redis configured (tests / dev) → a clean None, never a crash.
# ---------------------------------------------------------------------------
async def test_no_redis_configured_is_a_clean_none(client, resolver_spy) -> None:
    assert get_api_redis() is None

    r = await client.post("/api/v1/run-routing/compile", json={"text": "route it"})

    assert r.status_code == 400, r.text  # no model configured — not a 500
    assert resolver_spy[-1].get("redis") is None


# ---------------------------------------------------------------------------
# The other half of the wire: create_app must PUBLISH its redis client where
# the request handlers can reach it (before this fix it only bound the client
# to the live-event bus, which the resolvers cannot see).
# ---------------------------------------------------------------------------
async def test_create_app_publishes_redis_to_the_bus_and_the_api_accessor(monkeypatch) -> None:
    from backend.api import main as main_mod

    built: list[str] = []
    bound_to_bus: list[Any] = []
    client = _SentinelRedis()

    def _fake_from_url(url: str, **_kwargs: Any) -> Any:
        built.append(url)
        return client

    monkeypatch.setattr(main_mod.redis_aio, "from_url", _fake_from_url)
    monkeypatch.setattr(main_mod, "set_live_event_bus_redis", bound_to_bus.append)
    # ``create_app`` deliberately skips the redis bind under pytest (per-test
    # event loops + a process-wide client leak Futures across loops). Exercise
    # the bind seam directly, as production runs it.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class _S:
        redis_url = "redis://localhost:6379/0"

    main_mod.bind_process_redis(_S())  # type: ignore[arg-type]

    assert built == ["redis://localhost:6379/0"]
    assert bound_to_bus == [client]
    # The regression: the SAME client must also be reachable from the API
    # resolvers, not only from the SSE bus.
    assert get_api_redis() is client


async def test_bind_process_redis_is_a_noop_without_a_redis_url(monkeypatch) -> None:
    from backend.api import main as main_mod

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class _S:
        redis_url = ""

    main_mod.bind_process_redis(_S())  # type: ignore[arg-type]

    assert get_api_redis() is None
