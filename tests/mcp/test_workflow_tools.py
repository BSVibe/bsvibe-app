"""Workflow tool handler tests — Lift D2.

Exercises products / runs / deliverables tools end-to-end against an
in-memory SQLite DB. Each test seeds the row(s) it needs, constructs a
:class:`ToolContext` with a deterministic :class:`McpPrincipal`, calls
the tool, and asserts the typed output shape.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine
from ._rls import scoped_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("dispatch_publishes_workspace")]


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    async with scoped_session(db, workspace_id) as s:
        ws = WorkspaceRow(id=workspace_id, name="ws")
        s.add(ws)
        await s.commit()
        yield


async def test_products_list_returns_workspace_scoped_rows(
    db, workspace_id, user_id, registry, seeded
) -> None:
    other_ws = uuid.uuid4()
    async with scoped_session(db, other_ws) as s:
        s.add(WorkspaceRow(id=other_ws, name="other"))
        await s.flush()
        s.add(ProductRow(workspace_id=other_ws, name="X", slug="x"))
        await s.commit()
    async with scoped_session(db, workspace_id) as s:
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="a"))
        s.add(ProductRow(workspace_id=workspace_id, name="B", slug="b"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_products_list", {"limit": 50}, ctx)
    assert isinstance(out, list)
    slugs = {p["slug"] for p in out}
    assert slugs == {"a", "b"}


async def test_products_show_by_slug_and_uuid(db, workspace_id, user_id, registry, seeded) -> None:
    pid = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(ProductRow(id=pid, workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        by_slug = await registry.call_tool("bsvibe_products_show", {"slug_or_id": "a"}, ctx)
        by_uuid = await registry.call_tool("bsvibe_products_show", {"slug_or_id": str(pid)}, ctx)
    assert by_slug["slug"] == "a"
    assert by_uuid["slug"] == "a"
    assert by_uuid["id"] == str(pid)


async def test_products_show_other_workspace_not_found(
    db, workspace_id, user_id, registry, seeded
) -> None:
    other_ws = uuid.uuid4()
    async with scoped_session(db, other_ws) as s:
        s.add(WorkspaceRow(id=other_ws, name="other"))
        await s.flush()
        s.add(ProductRow(workspace_id=other_ws, name="X", slug="x"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="product not found"):
            await registry.call_tool("bsvibe_products_show", {"slug_or_id": "x"}, ctx)


async def test_products_create_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_products_create", {"name": "A", "slug": "a"}, ctx)


async def test_products_create_writes_row(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_products_create",
            {"name": "MCP Created", "slug": "mcp-created"},
            ctx,
        )
    assert out["slug"] == "mcp-created"
    assert out["name"] == "MCP Created"
    # Verify the row landed.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_products_list", {}, ctx)
    assert any(p["slug"] == "mcp-created" for p in listed)


async def test_runs_list_and_show(db, workspace_id, user_id, registry, seeded) -> None:
    run_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        run = ExecutionRun(
            id=run_id,
            workspace_id=workspace_id,
            product_id=None,
            status=RunStatus.RUNNING,
            payload={"intent_text": "ship a thing"},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(run)
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_runs_list", {}, ctx)
        shown = await registry.call_tool("bsvibe_runs_show", {"run_id": str(run_id)}, ctx)
    assert len(listed) == 1
    assert listed[0]["id"] == str(run_id)
    assert "intent" not in listed[0]
    assert listed[0]["title"] == "ship a thing"
    assert listed[0]["intent_preview"] == "ship a thing"
    assert listed[0]["intent_truncated"] is False
    assert shown["id"] == str(run_id)


async def _seed_run(db, workspace_id, *, status, product_id=None) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=product_id,
                status=status,
                payload={},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return run_id


async def test_runs_cancel_cancels_inflight(db, workspace_id, user_id, registry, seeded) -> None:
    run_id = await _seed_run(db, workspace_id, status=RunStatus.RUNNING)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_runs_cancel", {"run_id": str(run_id)}, ctx)
    assert out["cancelled"] is True
    assert out["status"] == "cancelled"
    async with scoped_session(db, workspace_id) as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.CANCELLED


async def test_runs_cancel_review_ready_errors(db, workspace_id, user_id, registry, seeded) -> None:
    """review_ready is not in-flight — cancel must error (guide to discard)."""
    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="discard|review_ready|in-flight"):
            await registry.call_tool("bsvibe_runs_cancel", {"run_id": str(run_id)}, ctx)


async def test_runs_discard_cancels_review_ready(
    db, workspace_id, user_id, registry, seeded
) -> None:
    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    async with scoped_session(db, workspace_id) as s:
        s.add(
            Deliverable(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={},
                compensation_handles=None,
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_runs_discard", {"run_id": str(run_id)}, ctx)
    assert out["cancelled"] is True
    assert out["status"] == "cancelled"
    assert len(out["deliverables_retracted"]) == 1
    async with scoped_session(db, workspace_id) as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.CANCELLED


async def test_runs_discard_unknown_errors(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool("bsvibe_runs_discard", {"run_id": str(uuid.uuid4())}, ctx)


async def test_runs_discard_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_runs_discard", {"run_id": str(run_id)}, ctx)


async def test_deliverables_list_filters_by_run(
    db, workspace_id, user_id, registry, seeded
) -> None:
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        for rid in (run_id, other_run_id):
            s.add(
                ExecutionRun(
                    id=rid,
                    workspace_id=workspace_id,
                    product_id=None,
                    status=RunStatus.SHIPPED,
                    payload={},
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        await s.flush()
        s.add(
            Deliverable(
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                artifact_uri="s3://x/y",
                payload={},
            )
        )
        s.add(
            Deliverable(
                workspace_id=workspace_id,
                run_id=other_run_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                artifact_uri="s3://x/z",
                payload={},
            )
        )
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        all_d = await registry.call_tool("bsvibe_deliverables_list", {}, ctx)
        filtered = await registry.call_tool(
            "bsvibe_deliverables_list", {"run_id": str(run_id)}, ctx
        )
    assert len(all_d) == 2
    assert len(filtered) == 1
    assert filtered[0]["run_id"] == str(run_id)


async def test_products_set_metadata_replaces_and_show_reflects(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """MCP parity — ``bsvibe_products_set_metadata`` REPLACES the free-form
    metadata dict; ``bsvibe_products_show`` reflects it and the row actually
    carries it (producer existence, name-clash-free ``product_metadata`` attr)."""
    pid = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(ProductRow(id=pid, workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()

    # A freshly seeded product shows an empty metadata object.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool("bsvibe_products_show", {"slug_or_id": "a"}, ctx)
    assert shown["metadata"] == {}

    # Set it (write scope).
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_products_set_metadata",
            {"slug_or_id": "a", "metadata": {"stage": "beta"}},
            ctx,
        )
    assert out["metadata"] == {"stage": "beta"}

    # show reflects the new value.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool("bsvibe_products_show", {"slug_or_id": "a"}, ctx)
    assert shown["metadata"] == {"stage": "beta"}

    # The row genuinely carries it under the SQLAlchemy-safe attribute name.
    async with scoped_session(db, workspace_id) as s:
        row = await s.get(ProductRow, pid)
        assert row.product_metadata == {"stage": "beta"}

    # REPLACE semantics — a second call overwrites the whole dict.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_products_set_metadata",
            {"slug_or_id": "a", "metadata": {"owner": "founder"}},
            ctx,
        )
    assert out["metadata"] == {"owner": "founder"}


async def test_products_show_resolves_execution_target(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """#692 — ``bsvibe_products_show`` surfaces the resolved ``execution_target``.

    A product that says nothing shows the safe default (``server_sandbox``);
    setting ``metadata.execution_target = client_attach`` flips the resolved
    field. Declared via the existing free-form metadata surface (no new column)."""
    pid = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(ProductRow(id=pid, workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()

    # Default: unset metadata → server_sandbox.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool("bsvibe_products_show", {"slug_or_id": "a"}, ctx)
    assert shown["execution_target"] == "server_sandbox"

    # Opt in to client_attach via the free-form metadata surface.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_products_set_metadata",
            {"slug_or_id": "a", "metadata": {"execution_target": "client_attach"}},
            ctx,
        )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool("bsvibe_products_show", {"slug_or_id": "a"}, ctx)
    assert shown["execution_target"] == "client_attach"


async def test_products_set_metadata_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with scoped_session(db, workspace_id) as s:
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_products_set_metadata",
                {"slug_or_id": "a", "metadata": {"x": 1}},
                ctx,
            )


async def test_runs_list_by_product_is_not_truncated_by_newer_other_runs(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Asking for a product's runs must not depend on how busy the rest of the
    workspace has been.

    The regression this pins: the product axis used to be a *client-side*
    filter applied to a workspace-wide page, so a product whose runs were
    older than ``limit`` other runs came back empty — the tool reported
    "no runs" for a product that has one. Scoping the query to the product
    is what makes the answer about the product.
    """
    product_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="P", slug="p"))
        await s.commit()

    # The product's only run is the OLDEST row in the workspace. The
    # timestamps are explicit rather than "whatever now() gave us" — the
    # ordering IS the condition under test, so it must not depend on how
    # fast the seeds ran.
    product_run = uuid.uuid4()
    base = datetime.now(UTC)
    async with scoped_session(db, workspace_id) as s:
        s.add(
            ExecutionRun(
                id=product_run,
                workspace_id=workspace_id,
                product_id=product_id,
                status=RunStatus.RUNNING,
                payload={},
                created_at=base - timedelta(hours=4),
                updated_at=base - timedelta(hours=4),
            )
        )
        for hours in (3, 2, 1):
            s.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    product_id=None,
                    status=RunStatus.RUNNING,
                    payload={},
                    created_at=base - timedelta(hours=hours),
                    updated_at=base - timedelta(hours=hours),
                )
            )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        # A page smaller than the number of newer, unrelated runs.
        listed = await registry.call_tool(
            "bsvibe_runs_list", {"product_slug_or_id": "p", "limit": 2}, ctx
        )

    assert [row["id"] for row in listed] == [str(product_run)]


async def test_runs_detail_returns_the_same_derivation_the_browser_gets(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """``bsvibe_runs_detail`` runs the REST route's builder, not a copy of it.

    The parity gap this closes was not "no tool" but "the rule lives somewhere
    MCP may not import". The assertion therefore checks a field the *builder*
    derives (``trigger``, read defensively out of the free-form payload) rather
    than a column the row already carries — a mirrored tool would have to
    re-derive that, and this is where the two would drift.
    """
    run_id = await _seed_run(db, workspace_id, status=RunStatus.RUNNING)
    async with scoped_session(db, workspace_id) as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        run.payload = {"intent_text": "ship the export", "frame": {"summary_title": "Export"}}
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        detail = await registry.call_tool("bsvibe_runs_detail", {"run_id": str(run_id)}, ctx)

    assert detail["id"] == str(run_id)
    assert detail["trigger"]["intent_text"] == "ship the export"
    # Read-only surfaces still answer for a run with nothing around it.
    assert detail["decisions"] == []
    assert detail["verification"] is None
    assert detail["deliverable_id"] is None


async def test_runs_detail_says_why_the_last_verification_failed(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A failed verdict used to read as ``{outcome: failed}`` and nothing else — the founder (and
    any reviewer) could not see which check broke or how (prod #1204: 8 rounds, no visible
    cause). The detail now carries the same failure text the agent is handed next round."""
    from backend.workflow.infrastructure.db import VerificationOutcome, VerificationResult

    run_id = await _seed_run(db, workspace_id, status=RunStatus.RUNNING)
    async with scoped_session(db, workspace_id) as s:
        s.add(
            VerificationResult(
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.FAILED,
                contract={},
                result={
                    "gate_expected": True,
                    "derived_gate": {
                        "origin": "derived",
                        "applicable": True,
                        "passed": False,
                        "commands": [
                            {
                                "command": "npx --yes pnpm@10 install",
                                "kind": "quality",
                                "status": "failed",
                                "exit_code": 1,
                                "output": "ERR_PNPM_IGNORED_BUILDS: esbuild",
                            }
                        ],
                    },
                },
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        detail = await registry.call_tool("bsvibe_runs_detail", {"run_id": str(run_id)}, ctx)

    failure = detail["verification"]["failure"]
    assert "npx --yes pnpm@10 install" in failure
    assert "ERR_PNPM_IGNORED_BUILDS" in failure


async def _detail_via_builder(db, workspace_id, run_id):
    from backend.workflow.application.run_detail import build_run_detail  # noqa: PLC0415
    from backend.workflow.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyDecisionRepository,
        SqlAlchemyDeliverableRepository,
        SqlAlchemyRunRepository,
    )

    async with db() as s:
        return await build_run_detail(
            run_id=run_id,
            workspace_id=workspace_id,
            session=s,
            runs=SqlAlchemyRunRepository(s),
            decisions=SqlAlchemyDecisionRepository(s),
            deliverables=SqlAlchemyDeliverableRepository(s),
        )


async def _seed_verification(db, workspace_id, run_id, outcome, result) -> None:
    from backend.workflow.infrastructure.db import VerificationResult  # noqa: PLC0415

    async with scoped_session(db, workspace_id) as s:
        s.add(
            VerificationResult(
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=outcome,
                contract={},
                result=result,
            )
        )
        await s.commit()


async def test_runs_detail_lists_floor_and_final_checks(db, workspace_id, seeded) -> None:
    from backend.workflow.infrastructure.db import VerificationOutcome  # noqa: PLC0415

    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    await _seed_verification(
        db,
        workspace_id,
        run_id,
        VerificationOutcome.PASSED,
        {
            "derived_gate": {
                "passed": True,
                "commands": [
                    {"command": "pytest", "kind": "test", "status": "passed", "exit_code": 0},
                    {"command": "make floor", "kind": "floor", "status": "passed", "exit_code": 0},
                    {"command": "make final", "kind": "final", "status": "unavailable"},
                ],
            },
            "requirements": [
                {"id": "R1", "status": "done"},
                {"id": "R2", "status": "deferred"},
                {"id": "R3", "status": "done"},
            ],
        },
    )

    detail = await _detail_via_builder(db, workspace_id, run_id)

    v = detail.verification
    assert v is not None
    by_kind = {c.kind: c for c in v.checks}
    assert by_kind["floor"].status == "passed"
    assert by_kind["floor"].exit_code == 0
    assert by_kind["final"].status == "unavailable"
    assert by_kind["final"].exit_code is None
    assert (v.requirements_total, v.requirements_done, v.requirements_deferred) == (3, 2, 1)


async def test_runs_detail_lists_requirement_rows(
    db, workspace_id, user_id, registry, seeded
) -> None:
    from backend.workflow.infrastructure.db import VerificationOutcome  # noqa: PLC0415

    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    await _seed_verification(
        db,
        workspace_id,
        run_id,
        VerificationOutcome.FAILED,
        {
            "requirements": [
                {
                    "id": "R1",
                    "text": "do it",
                    "kind": "do",
                    "status": "rejected",
                    "note": "wrong file",
                    "counterfactual": "vacuous",
                },
                {"id": "R2", "text": "guard", "kind": "guard", "status": "done"},
            ]
        },
    )

    detail = await _detail_via_builder(db, workspace_id, run_id)

    assert detail is not None and detail.verification is not None
    r1, r2 = detail.verification.requirements
    assert (r1.id, r1.text, r1.kind, r1.status) == ("R1", "do it", "do", "rejected")
    assert (r1.note, r1.counterfactual) == ("wrong file", "vacuous")
    assert (r2.status, r2.note, r2.counterfactual) == ("done", None, None)

    async with db() as s:
        out = await registry.call_tool(
            "bsvibe_runs_detail", {"run_id": str(run_id)}, _read_ctx(s, workspace_id, user_id)
        )
    mcp_r1, mcp_r2 = out["verification"]["requirements"]
    assert (mcp_r1["status"], mcp_r1["note"], mcp_r1["counterfactual"]) == (
        "rejected",
        "wrong file",
        "vacuous",
    )
    assert "counterfactual" not in mcp_r2


async def test_build_run_detail_reads_a_stored_verification_result_ledger(
    db, workspace_id, seeded
) -> None:
    from backend.workflow.application.run_detail import build_run_detail  # noqa: PLC0415
    from backend.workflow.infrastructure.db import (  # noqa: PLC0415
        VerificationOutcome,
        VerificationResult,
    )
    from backend.workflow.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyDecisionRepository,
        SqlAlchemyDeliverableRepository,
        SqlAlchemyRunRepository,
    )

    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    async with scoped_session(db, workspace_id) as s:
        s.add(
            VerificationResult(
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.FAILED,
                contract={},
                result={
                    "requirements": [
                        {
                            "id": "R1",
                            "text": "x" * 400,
                            "kind": "do",
                            "status": "unclear",
                            "note": "y" * 400,
                            "counterfactual": "unknown",
                        }
                    ]
                },
            )
        )
        await s.commit()

    async with db() as s:
        detail = await build_run_detail(
            run_id=run_id,
            workspace_id=workspace_id,
            session=s,
            runs=SqlAlchemyRunRepository(s),
            decisions=SqlAlchemyDecisionRepository(s),
            deliverables=SqlAlchemyDeliverableRepository(s),
        )

    assert detail is not None and detail.verification is not None
    (item,) = detail.verification.requirements
    assert (item.id, item.status, item.counterfactual) == ("R1", "unclear", "unknown")
    assert len(item.text) == 200 and item.note is not None and len(item.note) == 300


async def test_runs_detail_truncates_requirement_text_and_note(db, workspace_id, seeded) -> None:
    from backend.workflow.infrastructure.db import VerificationOutcome  # noqa: PLC0415

    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    await _seed_verification(
        db,
        workspace_id,
        run_id,
        VerificationOutcome.FAILED,
        {
            "requirements": [
                {
                    "id": "R1",
                    "text": "t" * 500,
                    "kind": "do",
                    "status": "missing",
                    "note": "n" * 900,
                }
            ]
        },
    )

    detail = await _detail_via_builder(db, workspace_id, run_id)

    assert detail is not None and detail.verification is not None
    (item,) = detail.verification.requirements
    assert len(item.text) == 200
    assert item.note is not None and len(item.note) == 300


async def test_runs_detail_checks_carry_no_output_and_truncate_commands(
    db, workspace_id, user_id, registry, seeded
) -> None:
    from backend.workflow.infrastructure.db import VerificationOutcome  # noqa: PLC0415

    run_id = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    await _seed_verification(
        db,
        workspace_id,
        run_id,
        VerificationOutcome.PASSED,
        {
            "derived_gate": {
                "passed": True,
                "commands": [
                    {
                        "command": "echo " + "x" * 500,
                        "kind": "quality",
                        "status": "passed",
                        "exit_code": 0,
                        "output": "SECRET_TOKEN=hunter2",
                    }
                ],
            }
        },
    )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        detail = await registry.call_tool("bsvibe_runs_detail", {"run_id": str(run_id)}, ctx)

    (check,) = detail["verification"]["checks"]
    assert "output" not in check
    assert len(check["command"]) == 200
    assert "hunter2" not in str(detail)


async def test_runs_detail_tolerates_legacy_and_missing_verification(
    db, workspace_id, seeded
) -> None:
    from backend.workflow.infrastructure.db import VerificationOutcome  # noqa: PLC0415

    bare = await _seed_run(db, workspace_id, status=RunStatus.RUNNING)
    detail = await _detail_via_builder(db, workspace_id, bare)
    assert detail is not None
    assert detail.verification is None

    legacy = await _seed_run(db, workspace_id, status=RunStatus.REVIEW_READY)
    await _seed_verification(
        db, workspace_id, legacy, VerificationOutcome.PASSED, {"command_results": []}
    )
    detail = await _detail_via_builder(db, workspace_id, legacy)
    assert detail is not None and detail.verification is not None
    assert detail.verification.checks == []
    assert detail.verification.requirements_total is None
    assert detail.verification.requirements == []


async def test_runs_detail_other_workspace_is_not_found(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A cross-workspace id is indistinguishable from an unknown one."""
    other_ws = uuid.uuid4()
    async with scoped_session(db, other_ws) as s:
        s.add(WorkspaceRow(id=other_ws, name="other"))
        await s.commit()
    run_id = await _seed_run(db, other_ws, status=RunStatus.RUNNING)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError) as err:
            await registry.call_tool("bsvibe_runs_detail", {"run_id": str(run_id)}, ctx)
    # Pinned to the *run* being unknown: a bare `raises(ToolError)` is also
    # satisfied by "unknown tool", so it would pass before the tool exists.
    assert str(run_id) in str(err.value)
    assert "unknown tool" not in str(err.value)


class _StubRetractHandler:
    """Records what it was asked to compensate; never touches a plugin."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def compensate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("upstream said no")
        return {"reverted": True}


async def _seed_retractable(db, workspace_id, run_id) -> uuid.UUID:
    deliverable_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "delivered"},
                compensation_handles=[
                    {"plugin": "slack", "artifact_type": "message", "handle": {"ts": "1"}}
                ],
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return deliverable_id


async def test_deliverables_retract_runs_the_rule_and_marks_the_row(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """The MCP write goes through the SAME rule the REST route uses.

    The handler that actually calls a plugin is injected — the MCP context may
    not import it — so the tool is asserted on what the *rule* does: fire one
    compensate per stored handle, then flip ``retracted_at``.
    """
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_retractable(db, workspace_id, run_id)
    handler = _StubRetractHandler()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            extras={"retract_handler": handler},
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_retract", {"deliverable_id": str(deliverable_id)}, ctx
        )

    assert out["retracted"] is True
    assert out["already_retracted"] is False
    assert [c["plugin"] for c in handler.calls] == ["slack"]
    async with scoped_session(db, workspace_id) as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None and row.retracted_at is not None


async def test_deliverables_retract_does_not_mark_the_row_when_compensate_fails(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """All-or-nothing: a failed dispatch leaves the row retractable."""
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_retractable(db, workspace_id, run_id)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            extras={"retract_handler": _StubRetractHandler(fail=True)},
        )
        with pytest.raises(ToolError) as err:
            await registry.call_tool(
                "bsvibe_deliverables_retract", {"deliverable_id": str(deliverable_id)}, ctx
            )
    assert "upstream said no" in str(err.value)

    async with scoped_session(db, workspace_id) as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None and row.retracted_at is None


async def test_deliverables_retract_without_an_injected_handler_refuses(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """``build_registry()`` is also called with no injection (server.py).

    The tool must still REGISTER on that path — dropping it would break the
    REST↔MCP parity guard — so absence has to surface at call time, as a clear
    refusal rather than a crash or a silent no-op that reports success.
    """
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_retractable(db, workspace_id, run_id)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError) as err:
            await registry.call_tool(
                "bsvibe_deliverables_retract", {"deliverable_id": str(deliverable_id)}, ctx
            )
    assert "unknown tool" not in str(err.value)

    async with scoped_session(db, workspace_id) as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None and row.retracted_at is None


class _StubNarrative:
    """A narrative generator that never touches an LLM."""

    def __init__(self, text: str | None = "it now exports the thing") -> None:
        self.text = text
        self.calls = 0

    async def narrate(self, _session: Any, **_kw: Any) -> str | None:
        self.calls += 1
        return self.text


async def _seed_reportable(db, workspace_id, run_id) -> uuid.UUID:
    deliverable_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "added an export"},
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return deliverable_id


async def test_deliverables_report_composes_the_same_proof_the_browser_gets(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """The tool runs the REST route's composition, not a copy.

    Asserted on ``verified`` — a field the *builder* decides (a real PASSED
    VerificationResult must exist; it is never inferred from the deliverable
    existing). A mirrored tool would have to re-derive that, and that is where
    the two would drift.
    """
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_reportable(db, workspace_id, run_id)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            extras={"narrative_generator": _StubNarrative()},
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_report", {"deliverable_id": str(deliverable_id)}, ctx
        )

    assert out["deliverable"]["id"] == str(deliverable_id)
    # No PASSED verification was recorded → honestly needs-review, not "verified".
    assert out["verified"] is False
    assert out["verifications"] == []


async def test_deliverables_report_without_a_generator_still_returns_the_proof(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A report is a READ: no generator wired → degrade, never refuse.

    This is the opposite of retract (a write), where a missing runtime must
    refuse rather than claim a rollback that never happened. Here the founder
    still gets the deliverable, its verifications and its references — only the
    freshly-worded sentence is missing.
    """
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_reportable(db, workspace_id, run_id)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_report", {"deliverable_id": str(deliverable_id)}, ctx
        )

    assert out["deliverable"]["id"] == str(deliverable_id)
    assert out["narrative"] is None


async def test_deliverables_report_other_workspace_is_not_found(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A cross-workspace id is indistinguishable from an unknown one."""
    other_ws = uuid.uuid4()
    async with scoped_session(db, other_ws) as s:
        s.add(WorkspaceRow(id=other_ws, name="other-report"))
        await s.commit()
    run_id = await _seed_run(db, other_ws, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_reportable(db, other_ws, run_id)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError) as err:
            await registry.call_tool(
                "bsvibe_deliverables_report", {"deliverable_id": str(deliverable_id)}, ctx
            )
    # Pinned to the *deliverable* being unknown — a bare raises() is also
    # satisfied by "unknown tool", i.e. it would pass before the tool exists.
    assert str(deliverable_id) in str(err.value)
    assert "unknown tool" not in str(err.value)


async def _seed_with_artifact(db, workspace_id, run_id, tmp_path, *, ref: str, body: bytes):
    """Seed a deliverable declaring ``ref`` and put ``body`` in the run dir."""
    run_dir = tmp_path / str(run_id)
    target = run_dir / ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    deliverable_id = uuid.uuid4()
    async with scoped_session(db, workspace_id) as s:
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "built", "artifact_refs": [ref]},
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return deliverable_id


async def test_deliverables_artifacts_serves_a_declared_ref(
    db, workspace_id, user_id, registry, seeded, tmp_path, monkeypatch
) -> None:
    """The tool reads a declared artifact through the shared rule."""
    monkeypatch.setenv("BSVIBE_RUN_WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_with_artifact(
        db, workspace_id, run_id, tmp_path, ref="src/app.py", body=b"print('hi')\n"
    )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_artifacts",
            {"deliverable_id": str(deliverable_id), "ref": "src/app.py"},
            ctx,
        )
    get_settings.cache_clear()

    assert out["ref"] == "src/app.py"
    assert out["content"] == "print('hi')\n"
    assert out["binary"] is False
    assert out["truncated"] is False


async def test_deliverables_artifacts_refuses_a_ref_the_deliverable_never_declared(
    db, workspace_id, user_id, registry, seeded, tmp_path, monkeypatch
) -> None:
    """The ref whitelist travels WITH the rule — the tool cannot be laxer.

    The file exists on disk and is readable; the ONLY reason it is refused is
    that this deliverable never declared it. Without the whitelist a workspace
    token would read any path under the run dir.
    """
    monkeypatch.setenv("BSVIBE_RUN_WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_with_artifact(
        db, workspace_id, run_id, tmp_path, ref="src/app.py", body=b"declared\n"
    )
    # A real, readable neighbour that this deliverable does NOT declare.
    (tmp_path / str(run_id) / ".env").write_bytes(b"SECRET=1\n")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError) as err:
            await registry.call_tool(
                "bsvibe_deliverables_artifacts",
                {"deliverable_id": str(deliverable_id), "ref": ".env"},
                ctx,
            )
    get_settings.cache_clear()
    assert "unknown tool" not in str(err.value)


async def test_deliverables_artifacts_reports_binary_without_the_bytes(
    db, workspace_id, user_id, registry, seeded, tmp_path, monkeypatch
) -> None:
    """A binary artifact yields a note, never raw bytes."""
    monkeypatch.setenv("BSVIBE_RUN_WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    deliverable_id = await _seed_with_artifact(
        db, workspace_id, run_id, tmp_path, ref="logo.png", body=b"\x89PNG\x00\x01\x02"
    )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_artifacts",
            {"deliverable_id": str(deliverable_id), "ref": "logo.png"},
            ctx,
        )
    get_settings.cache_clear()

    assert out["binary"] is True
    assert "\x00" not in out["content"]
    assert "bytes" in out["content"]


async def _seed_runs(db, workspace_id, specs) -> list[uuid.UUID]:
    """Store runs ``(status, intent)`` with strictly decreasing recency."""
    base = datetime.now(UTC)
    ids: list[uuid.UUID] = []
    async with scoped_session(db, workspace_id) as s:
        for i, (status, intent) in enumerate(specs):
            run_id = uuid.uuid4()
            ids.append(run_id)
            s.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=workspace_id,
                    status=status,
                    payload={"intent_text": intent},
                    created_at=base - timedelta(minutes=i),
                    updated_at=base - timedelta(minutes=i),
                )
            )
        await s.commit()
    return ids


def _read_ctx(s, workspace_id, user_id) -> ToolContext:
    return ToolContext(
        principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
        session=s,
    )


async def test_runs_list_omits_intent_and_stays_small(
    db, workspace_id, user_id, registry, seeded
) -> None:
    import json

    intents = [f"# Issue {i}\n" + "x" * 10_000 for i in range(20)]
    ids = await _seed_runs(db, workspace_id, [(RunStatus.RUNNING, t) for t in intents])

    async with db() as s:
        ctx = _read_ctx(s, workspace_id, user_id)
        listed = await registry.call_tool("bsvibe_runs_list", {"limit": 20}, ctx)
        shown = await registry.call_tool("bsvibe_runs_show", {"run_id": str(ids[3])}, ctx)

    assert len(listed) == 20
    assert len(json.dumps(listed)) < 20_000
    for item in listed:
        assert "intent" not in item
        assert item["intent_truncated"] is True
        assert len(item["intent_preview"]) == 300
    assert listed[3]["title"] == "# Issue 3"
    assert shown["intent"] == intents[3]
    assert shown["title"] == "# Issue 3"


async def test_run_title_is_first_content_line(db, workspace_id, user_id, registry, seeded) -> None:
    long_line = "y" * 500
    await _seed_runs(
        db,
        workspace_id,
        [(RunStatus.RUNNING, "\n\n   \n  Fix the login  \nbody"), (RunStatus.RUNNING, long_line)],
    )
    async with db() as s:
        listed = await registry.call_tool(
            "bsvibe_runs_list", {}, _read_ctx(s, workspace_id, user_id)
        )
    assert [r["title"] for r in listed] == ["Fix the login", "y" * 120]


async def test_runs_list_status_filter_applies_before_limit(
    db, workspace_id, user_id, registry, seeded
) -> None:
    # Newest 10 are not shipped; the 3 shipped runs are older than all of them.
    specs = [(RunStatus.RUNNING, "r")] * 10 + [(RunStatus.SHIPPED, "s")] * 3
    ids = await _seed_runs(db, workspace_id, specs)

    async with db() as s:
        ctx = _read_ctx(s, workspace_id, user_id)
        two = await registry.call_tool("bsvibe_runs_list", {"status": "shipped", "limit": 2}, ctx)
        all_shipped = await registry.call_tool(
            "bsvibe_runs_list", {"status": "shipped", "limit": 50}, ctx
        )
        with pytest.raises(ToolError, match="unknown status"):
            await registry.call_tool("bsvibe_runs_list", {"status": "bogus"}, ctx)

    assert [r["id"] for r in two] == [str(i) for i in ids[10:12]]
    assert {r["status"] for r in all_shipped} == {"shipped"}
    assert len(all_shipped) == 3
