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
from typing import Any

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


def _code_only(text: str) -> str:
    """``path`` 의 내용에서 주석과 독스트링을 걷어낸 것.

    부재 가드는 **코드**를 재야 한다. 이 모듈의 독스트링들은 일부러 폐기된 키의
    이름을 적어 내력을 남기고 있고(원본은 히스토리다), 그걸 위반으로 세면 가드가
    자기 설명에 걸려 빨개진다 — 그러면 다음 사람이 가드를 느슨하게 만든다.

    정규식 스트리퍼라 완벽하지 않다. 위 양성 대조군이 스트리퍼가 파일을 통째로
    삼키지 않았음을 매 런 확인한다.
    """
    import re

    for opener in (chr(34) * 3, chr(39) * 3):
        text = re.sub(re.escape(opener) + r"(?:.|\n)*?" + re.escape(opener), "", text)
    text = re.sub(r"/\*(?:.|\n)*?\*/", "", text)
    text = re.sub(r"^[ \t]*(?:#|//).*$", "", text, flags=re.MULTILINE)
    return text


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
        # The mirror this guard never crossed. #924 closed the Python half and
        # left the PWA writing the dead key into a PATCH that REST now 422s —
        # a guard written in one language proves nothing about the other.
        root / "apps/pwa/lib/api/types.ts",
        root / "apps/pwa/components/products/ProductBindings.tsx",
    ]
    missing = [p.name for p in surface if not p.exists()]
    assert not missing, f"the surface moved — this guard now watches nothing: {missing}"

    # Prose may narrate the removal; code may not spell the key. Without the
    # split this guard cries wolf at its own explanation — and a guard that
    # fails for the wrong reason gets loosened, then it guards nothing.
    # Word-anchored: ``quiet_hours_enabled`` lives in the same ``types.ts`` and
    # is none of this guard's business. A substring match made the guard fail on
    # an unrelated field, and a guard that fails for the wrong reason gets
    # deleted by the next person who trips it.
    import re as _re

    spellings = (
        r'"enabled"',
        r"'enabled'",
        r"\benabled\s*:",
        r"\benabled\s*=",
        r"\{enabled\b",
        r"\.enabled\b",
    )
    offenders = {
        p.name: [s for s in spellings if _re.search(s, _code_only(p.read_text()))]
        for p in surface
        if any(_re.search(s, _code_only(p.read_text())) for s in spellings)
    }
    assert not offenders, f"the dead trigger knob came back in: {offenders}"


# ---------------------------------------------------------------------------
# The producers — enumerated by what the runtime builds, not by where I looked
# ---------------------------------------------------------------------------
def _binding_tools_with_a_trigger_knob() -> list[Any]:
    """Every registered MCP tool whose input carries the ``trigger`` knob.

    Enumerated from the **live registry** rather than a hand-written list: a
    list proves only the surfaces I happened to think of, and this key survived
    #924 precisely in one I didn't. A fifth binding tool added tomorrow lands in
    this set for free.
    """
    from backend.mcp.server import build_registry

    registry = build_registry()
    return [
        registry.get(name)
        for name in registry.names()
        if (tool := registry.get(name)) is not None and "trigger" in tool.input_schema.model_fields
    ]


def test_the_trigger_carrying_tool_set_is_not_empty() -> None:
    """The size assertion — an empty set passes every test below vacuously."""
    tools = _binding_tools_with_a_trigger_knob()
    assert {t.name for t in tools} == {
        "bsvibe_bindings_create",
        "bsvibe_bindings_update",
    }, [t.name for t in tools]


#: Minimal VALID arguments per tool — every field the model requires and no
#: other. Feeding both models the union makes ``extra="forbid"`` raise on the
#: wrong field, and the refusal test then passes without ever reaching the knob.
_MINIMAL_ARGS: dict[str, dict[str, Any]] = {
    "bsvibe_bindings_create": {
        "product_id": "11111111-1111-1111-1111-111111111111",
        "connector_account_id": "22222222-2222-2222-2222-222222222222",
        "resource_id": "chat-1",
    },
    "bsvibe_bindings_update": {
        "binding_id": "33333333-3333-3333-3333-333333333333",
    },
}


@pytest.mark.parametrize("tool_name", ["bsvibe_bindings_create", "bsvibe_bindings_update"])
def test_every_trigger_carrying_tool_refuses_the_dead_key(tool_name: str) -> None:
    """MCP must refuse what REST refuses.

    ``TriggerKnob(extra="forbid")`` closed the REST door in #924; the MCP mirror
    kept an untyped ``dict[str, Any]`` and its own docstring claims to be a
    ``Mirror of ResourceBindingCreate … 1:1``. It wasn't — and prod's 09-18
    binding, created a week AFTER the removal, carries the dead key because of
    it. The founder's own principle: MCP schemas match REST ``extra=forbid``.
    """
    from pydantic import ValidationError

    from backend.mcp.server import build_registry

    tool = build_registry().get(tool_name)
    assert tool is not None
    # Control: the same arguments WITHOUT the dead key must validate, or the
    # refusal below proves only that some unrelated field was wrong.
    tool.input_schema.model_validate(dict(_MINIMAL_ARGS[tool_name]))
    with pytest.raises(ValidationError):
        tool.input_schema.model_validate(
            {**_MINIMAL_ARGS[tool_name], "trigger": {"enabled": False, "filters": {}}}
        )


@pytest.mark.parametrize("tool_name", ["bsvibe_bindings_create", "bsvibe_bindings_update"])
def test_the_live_half_still_passes(tool_name: str) -> None:
    """Negative control — refusing everything would pass the test above."""
    from backend.mcp.server import build_registry

    tool = build_registry().get(tool_name)
    assert tool is not None
    args = tool.input_schema.model_validate(
        {**_MINIMAL_ARGS[tool_name], "trigger": {"filters": {"action": "opened"}}}
    )
    assert args.trigger is not None
    assert args.trigger.filters == {"action": "opened"}


def test_no_tool_advertises_the_dead_key_to_an_agent() -> None:
    """What an agent reads is the wire description, not our source comments.

    ``bsvibe_bindings_create`` described the knob as ``({enabled, filters})``
    for eleven days after the key was deleted. An agent that believes the menu
    writes the dead key, and an untyped dict stores it — the schema fix above
    and this one are two halves of the same door.

    Scoped to the tools that carry a ``trigger``: ``bsvibe_schedules_set_enabled``
    has a legitimate ``enabled`` and must not fail this.
    """
    from backend.mcp.server import build_registry

    registry = build_registry()
    wire = {t.name: t for t in registry.list_tools()}
    offenders = []
    for tool in _binding_tools_with_a_trigger_knob():
        spec = wire[tool.name]
        blob = f"{spec.description}\n{spec.inputSchema}"
        if "enabled" in blob:
            offenders.append(tool.name)
    assert not offenders, f"the dead knob is still on the menu for: {offenders}"


def test_the_migration_that_resets_the_column_default_exists() -> None:
    """The layer below the one #924 fixed.

    #924 changed the Python default; the DDL ``server_default`` kept the old
    shape, so any INSERT that omits the column is answered by Postgres with the
    dead key — the e2e checklist filed that as "harmless but noted", which is
    exactly what a value nothing reads always looks like.

    This asserts the reset revision is present in the chain. What the DDL
    actually ends up as is asserted against a live Postgres in
    ``tests/test_alembic_fresh.py`` — source text is not a database.
    """
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "backend/data/migrations/versions"
    files = sorted(versions.glob("*.py"))
    assert len(files) > 20, "migration set collapsed — this scan proves nothing"
    resets = [p.name for p in files if "trigger_default" in p.name]
    assert resets, "no revision resets resource_bindings.trigger's DDL default"


def test_the_stripper_keeps_code_and_drops_prose() -> None:
    """The control for the guard above — it can only fail in one direction.

    ``_code_only`` deciding everything is a comment would make
    ``test_no_module_reads_the_dead_key`` pass forever without reading a line of
    code. So assert both halves on a probe that carries the dead key in prose
    AND in code: the prose must survive nowhere, the code must survive.
    """
    dq, sq = chr(34) * 3, chr(39) * 3
    probe = "\n".join(
        [
            f"{dq}Module doc: it once carried enabled: False.{dq}",
            "# comment: trigger.enabled is gone",
            "// ts comment: enabled: boolean",
            "/* block: {enabled, filters} */",
            f"def f():  {sq}inner doc mentioning enabled{sq}",
            'CODE = {"enabled": False}',
        ]
    )
    stripped = _code_only(probe)
    assert 'CODE = {"enabled": False}' in stripped, "the stripper ate the code"
    assert "Module doc" not in stripped
    assert "ts comment" not in stripped
    assert "block:" not in stripped
    assert "inner doc" not in stripped
    assert stripped.count("enabled") == 1, stripped
