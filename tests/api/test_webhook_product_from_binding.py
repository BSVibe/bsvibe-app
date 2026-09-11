"""An inbound chat message must land ON a product — via its binding.

Founder, 2026-09-11: *"당연히 제품 별로 봇, 이슈, 채널 등을 다 분리할 수 있어야
bsvibe 제품의 철학에 맞아."*

``resource_bindings`` is documented as a **"Per-Product × Connector 3-knob
binding"** and its index docstring names this exact call site:

    "the (connector_account_id, resource_id) index is what Receive (B10b) will
    use to resolve an inbound webhook → binding → Product"

The repository helper for it (``find_binding``) exists, says *"Receive-stage
lookup"*, and **had zero callers**. Prod already carried the founder's data —
``BStockReport × telegram`` with ``resource_id = '8242700007'`` — and nothing
read it.

**Why this is structural, not one connector being behind.** No plugin sets
``product_id`` (measured: ``product`` appears 0 times in the telegram / discord /
slack / github / sentry parsers). The only product resolver was the route's
``_product_id_for_repo``, whose input is ``payload["repo"]`` or the account's
``external_ref``. So **github worked only because its parser emits a repo**, and
every connector without a repo concept — every chat connector — was
product-less by construction. A run with no product does not clone anything; the
route's own comment calls that "running unbound in an empty workspace".

Fixing telegram alone would leave discord and slack on the same floor, so the
lookup is keyed off a per-connector resource key with a guard (below) that a new
connector cannot silently skip.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.webhooks import _product_id_from_binding, _resource_id_for
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    session: AsyncSession, *, connector: str, resource_id: str
) -> tuple[ConnectorAccountRow, uuid.UUID]:
    """A workspace with a product bound to one connector account."""
    ws = uuid.uuid4()
    session.add(WorkspaceRow(id=ws, name="WS", language="ko"))
    # Flush each FK LEVEL before the rows that point at it. SQLAlchemy orders
    # inserts per-mapper, not by cross-model FK dependency, and SQLite's lax FK
    # enforcement hides every violation here until PostgreSQL runs it.
    await session.flush()
    product = ProductRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        name="BStockReport",
        slug=f"p-{uuid.uuid4().hex[:8]}",
        repo_url="https://github.com/blas1n/BStockReport",
    )
    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        connector=connector,
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ct",
        delivery_config={},
        is_active=True,
    )
    session.add(product)
    session.add(account)
    # Flush the FK targets before the row that points at them — SQLAlchemy orders
    # inserts per-mapper, not by cross-model FK dependency, and SQLite's lax FK
    # enforcement hides the resulting violation until CI runs it on PostgreSQL.
    await session.flush()
    session.add(
        ResourceBindingRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            product_id=product.id,
            connector_account_id=account.id,
            resource_id=resource_id,
        )
    )
    await session.commit()
    return account, product.id


# ── the resource key each connector speaks ───────────────────────────────────


def test_each_chat_connector_declares_where_its_resource_id_lives() -> None:
    """Measured payload keys: telegram ``chat_id``, discord ``channel_id``,
    slack ``channel``, github ``repo``."""
    assert _resource_id_for("telegram", {"chat_id": -1003257931284}) == "-1003257931284"
    assert _resource_id_for("discord", {"channel_id": "C1"}) == "C1"
    assert _resource_id_for("slack", {"channel": "C2"}) == "C2"
    assert _resource_id_for("github", {"repo": "blas1n/BStockReport"}) == "blas1n/BStockReport"
    # A Sentry project is the same granularity — the guard below caught this one
    # missing, which is the guard doing its job.
    assert _resource_id_for("sentry", {"project": "bstockreport"}) == "bstockreport"


def test_a_numeric_resource_id_is_compared_as_a_string() -> None:
    """Telegram sends ``chat_id`` as a JSON number; the founder typed a string
    into the binding. Comparing the raw types would never match."""
    assert _resource_id_for("telegram", {"chat_id": 8242700007}) == "8242700007"


def test_an_unknown_connector_yields_no_resource_id() -> None:
    """A connector with no declared key must resolve to nothing rather than
    guess a field — a wrong guess binds a message to the wrong product."""
    assert _resource_id_for("mystery-connector", {"whatever": "x"}) is None
    # A declared connector whose event simply lacks the field is the same answer.
    assert _resource_id_for("telegram", {}) is None


def test_every_webhook_connector_has_a_declared_resource_key() -> None:
    """The guard that makes the fix travel.

    Fixing telegram alone would leave discord and slack product-less on exactly
    the same floor. A connector that can receive a webhook but declares no
    resource key silently falls back to "no product" — so adding one must fail
    HERE, with the reason attached, rather than in a founder's chat months later.
    """
    from backend.api.webhooks import _RESOURCE_ID_KEYS
    from backend.connectors.catalog import get_connector_catalog

    receiving = {
        name
        for name, info in get_connector_catalog().items()
        if info.webhook_trigger and info.user_connectable
    }
    missing = receiving - set(_RESOURCE_ID_KEYS)
    assert not missing, f"webhook connectors with no resource key declared: {sorted(missing)}"


# ── the lookup itself ────────────────────────────────────────────────────────


async def test_a_telegram_message_lands_on_its_bound_product(sf) -> None:
    """The founder's prod data, exercised: BStockReport × telegram × chat_id."""
    async with sf() as session:
        account, product_id = await _seed(session, connector="telegram", resource_id="8242700007")
        resolved = await _product_id_from_binding(
            session, account=account, payload={"chat_id": 8242700007, "text": "이거 해줘"}
        )
    assert resolved == product_id


@pytest.mark.parametrize(
    ("connector", "key", "value"),
    [("discord", "channel_id", "C-discord"), ("slack", "channel", "C-slack")],
)
async def test_the_other_chat_connectors_resolve_too(sf, connector, key, value) -> None:
    """The lesson must travel — these were product-less for the same reason."""
    async with sf() as session:
        account, product_id = await _seed(session, connector=connector, resource_id=value)
        resolved = await _product_id_from_binding(session, account=account, payload={key: value})
    assert resolved == product_id


async def test_an_unbound_resource_resolves_to_nothing(sf) -> None:
    """A message from a chat nobody bound must not guess a product.

    Prod's binding names the founder's 1:1 chat; a message from a group carries a
    different ``chat_id``. Silently picking the workspace's only product would
    work today and mis-route the moment there are two.
    """
    async with sf() as session:
        account, _product_id = await _seed(session, connector="telegram", resource_id="8242700007")
        resolved = await _product_id_from_binding(
            session, account=account, payload={"chat_id": -1003257931284}
        )
    assert resolved is None


async def test_another_accounts_binding_is_not_used(sf) -> None:
    """Two workspaces can bind the same chat id string. The lookup is keyed on
    the ACCOUNT the webhook token already resolved, so it cannot cross over."""
    async with sf() as session:
        _owner_account, _pid = await _seed(session, connector="telegram", resource_id="shared-id")
        stranger_account, _spid = await _seed(
            session, connector="telegram", resource_id="something-else"
        )
        resolved = await _product_id_from_binding(
            session, account=stranger_account, payload={"chat_id": "shared-id"}
        )
    assert resolved is None


async def test_a_connector_with_no_binding_at_all_is_quiet(sf) -> None:
    """No binding is the normal state for a fresh connector — not an error."""
    async with sf() as session:
        ws = uuid.uuid4()
        session.add(WorkspaceRow(id=ws, name="WS", language="ko"))
        await session.flush()
        account = ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            connector="telegram",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext="ct",
            delivery_config={},
            is_active=True,
        )
        session.add(account)
        await session.commit()
        resolved = await _product_id_from_binding(session, account=account, payload={"chat_id": 1})
    assert resolved is None


async def test_github_still_resolves_by_repo(sf) -> None:
    """Negative control: the ONE path that worked must keep working.

    github resolves through ``_product_id_for_repo`` on ``payload["repo"]``; a
    binding lookup that shadowed it would change behaviour for the connector
    that was never broken.
    """
    from backend.api.webhooks import _product_id_for_repo

    async with sf() as session:
        _account, product_id = await _seed(
            session, connector="github", resource_id="unused-for-this-test"
        )
        ws = (await session.get(ProductRow, product_id)).workspace_id
        resolved = await _product_id_for_repo(session, ws, "blas1n/BStockReport")
    assert resolved == product_id


async def test_the_binding_wins_over_the_repo_fallback(sf: Any) -> None:
    """An explicit binding is the founder's stated intent; the repo fallback is
    an inference. When both could answer, the stated one must win."""
    async with sf() as session:
        account, bound_product = await _seed(
            session, connector="github", resource_id="blas1n/Other"
        )
        resolved = await _product_id_from_binding(
            session, account=account, payload={"repo": "blas1n/Other"}
        )
    assert resolved == bound_product


def test_the_route_asks_the_binding_before_the_repo_fallback() -> None:
    """The wiring, pinned — every test above calls the resolver DIRECTLY.

    A resolver nothing calls changes nothing: the founder's binding sits in the
    table exactly as it did before, and every chat message still opens a run with
    no product. That hole is invisible to a unit test of the function itself,
    which is how this whole class of gap survived — ``find_binding`` was written,
    documented as the Receive-stage lookup, indexed for it, and never called.

    Order matters too. An explicit binding is the founder's stated intent; the
    repo path is an inference from the event. If the fallback ran first, a github
    event whose repo matches one product would silently outrank a binding the
    founder made to a different one.
    """
    import inspect

    from backend.api import webhooks

    source = inspect.getsource(webhooks.receive_connector_webhook)
    assert "_product_id_from_binding" in source, "the route never asks the binding"
    assert source.index("_product_id_from_binding") < source.index("_product_id_for_repo"), (
        "the repo inference must not outrank the founder's explicit binding"
    )
