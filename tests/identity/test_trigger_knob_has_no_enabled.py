"""``trigger.enabled`` is gone — a knob nothing implemented.

Measured 2026-09-11: ``resource_bindings.trigger`` is documented as
``{"enabled": bool, "filters": dict}`` — *"the do I act knob"*. Consumers:

* ``filters`` → read by the Receive stage (``stages/intake.py:179``), live.
* ``enabled`` → **zero readers.** The only hits were the model docstring, the
  API schema, and the migration that created it.

So a founder could toggle it in the API and the trigger kept firing. That is the
``config-menu-offers-options-nothing-implements`` shape: a control that reports
success and changes nothing.

**Why delete rather than wire it up.** ``filters`` already expresses "do I act" —
an empty filter passes everything, a non-matching one rejects. ``enabled`` is a
second axis layered on the first, and its stored default is ``False``: wiring it
would have silently switched OFF both of prod's bindings, including the telegram
path restored one hour earlier in #922. A knob whose activation is a regression
is a knob nobody designed the rest of the system around.

The COLUMN stays — ``filters`` lives in it. Only the dead key goes.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.infrastructure.repositories.resource_binding_repository_sql import (
    SqlAlchemyResourceBindingRepository,
)
from backend.identity.workspaces_db import ProductRow, WorkspaceRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws = uuid.uuid4()
    session.add(WorkspaceRow(id=ws, name="WS", language="ko"))
    # Flush each FK LEVEL before the rows pointing at it — SQLite's lax FK
    # enforcement hides the violation until PostgreSQL runs it.
    await session.flush()
    product = ProductRow(
        id=uuid.uuid4(), workspace_id=ws, name="P", slug=f"p-{uuid.uuid4().hex[:8]}"
    )
    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        connector="telegram",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ct",
        delivery_config={"chat_id": "1"},
        is_active=True,
    )
    session.add(product)
    session.add(account)
    await session.flush()
    return ws, product.id, account.id


async def test_a_fresh_binding_carries_no_enabled_key(sf) -> None:
    """The default is where the dead knob was born."""
    async with sf() as session:
        ws, product_id, account_id = await _seed(session)
        row = await SqlAlchemyResourceBindingRepository(session).create(
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="chat-1",
        )
        await session.commit()

    assert "enabled" not in row.trigger
    assert row.trigger == {"filters": {}}


async def test_the_filters_knob_still_works(sf) -> None:
    """Negative control: the HALF of this column that has a live consumer.

    ``stages/intake.py`` reads ``filters`` to reject a non-matching trigger, and
    deleting the wrong key here would silently stop that gate.
    """
    async with sf() as session:
        ws, product_id, account_id = await _seed(session)
        repo = SqlAlchemyResourceBindingRepository(session)
        row = await repo.create(
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="chat-1",
            trigger={"filters": {"action": "opened"}},
        )
        await session.commit()

    assert row.trigger["filters"] == {"action": "opened"}


async def test_a_caller_that_still_sends_enabled_is_refused(sf) -> None:
    """An old client must not silently write a key nothing reads.

    Accepting and storing it would recreate exactly the state this removes — a
    value in the database that looks like configuration and governs nothing.
    """
    from pydantic import ValidationError

    from backend.api.v1.products._schemas import TriggerKnob

    with pytest.raises(ValidationError):
        TriggerKnob(enabled=True, filters={})


def test_the_receive_stage_never_consulted_it() -> None:
    """The claim this deletion rests on, asserted rather than remembered.

    If a future change starts reading ``enabled`` again it must do so
    deliberately — reintroducing a knob whose stored default switches off the
    founder's live bindings should not happen by accident.
    """
    import inspect

    from backend.workflow.application.stages import intake

    source = inspect.getsource(intake)
    assert "filters" in source, "the live half of the knob must still be read"
    assert '"enabled"' not in source
    assert 'trig_cfg.get("enabled")' not in source


def test_no_module_reads_the_dead_key() -> None:
    """The guard that keeps it dead.

    A grep at review time is a moment; this is every run. Scoped to the binding
    surface so an unrelated ``enabled`` elsewhere in the codebase cannot fail it.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    surface = [
        root / "backend/identity/workspaces_db.py",
        root / "backend/identity/infrastructure/repositories/resource_binding_repository_sql.py",
        root / "backend/api/v1/products/_schemas.py",
        root / "backend/mcp/tools/bindings_tools.py",
        root / "backend/workflow/application/stages/intake.py",
    ]
    offenders = [p.name for p in surface if '"enabled"' in p.read_text()]
    assert not offenders, f"the dead trigger knob came back in: {offenders}"
