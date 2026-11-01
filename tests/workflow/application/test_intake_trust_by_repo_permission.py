"""A founder whose org membership is private is still trusted to steer their own run.

Prod #1279 (2026-10-30): the founder edited the issue of a run in flight and intake logged
``issue_edit_not_routed_untrusted_editor`` with ``author_association: CONTRIBUTOR``. On an
org-owned repo GitHub reports a member whose membership is private as CONTRIBUTOR, so the
association alone refused the founder — every edit and every PR comment became a new request
instead of reaching the run.

So when the association is not already trusted, intake asks GitHub for the person's
permission on the repo; ``admin`` / ``maintain`` / ``write`` is trusted. A lookup that fails,
or that is not wired, trusts no one beyond the association — as before.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.workflow.application.intake.issue_edit import route_issue_edit_to_run
from backend.workflow.application.intake.pr_comment import route_pr_comment_to_run
from backend.workflow.application.intake.trust import is_trusted
from backend.workflow.infrastructure.db import RunStatus
from plugin.github.client import GithubClient
from tests._support import db_engine

from . import test_issue_edit_routing as edits
from . import test_pr_comment_routing as comments

pytestmark = pytest.mark.asyncio


def _lookup(answer: str | None, calls: list[tuple[uuid.UUID, str, str]] | None = None):  # type: ignore[no-untyped-def]
    async def permission_of(workspace_id: uuid.UUID, repo: str, login: str) -> str | None:
        if calls is not None:
            calls.append((workspace_id, repo, login))
        return answer

    return permission_of


async def _boom(workspace_id: uuid.UUID, repo: str, login: str) -> str | None:
    raise RuntimeError("github down")


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("permission", ["admin", "maintain", "write"])
async def test_a_contributor_with_write_access_is_trusted(permission: str) -> None:
    ws = uuid.uuid4()
    assert await is_trusted(
        "CONTRIBUTOR",
        login="blas1n",
        repo="acme/app",
        workspace_id=ws,
        permission_of=_lookup(permission),
    )


@pytest.mark.parametrize("permission", ["read", "triage", "none", None])
async def test_without_write_access_a_contributor_is_not_trusted(permission: str | None) -> None:
    assert not await is_trusted(
        "CONTRIBUTOR",
        login="rando",
        repo="acme/app",
        workspace_id=uuid.uuid4(),
        permission_of=_lookup(permission),
    )


async def test_a_failed_lookup_or_no_lookup_trusts_only_the_association() -> None:
    ws = uuid.uuid4()
    assert not await is_trusted(
        "CONTRIBUTOR", login="x", repo="acme/app", workspace_id=ws, permission_of=_boom
    )
    assert not await is_trusted(
        "CONTRIBUTOR", login="x", repo="acme/app", workspace_id=ws, permission_of=None
    )
    assert await is_trusted(
        "OWNER", login="x", repo="acme/app", workspace_id=ws, permission_of=None
    )


async def test_a_trusted_association_does_not_call_github() -> None:
    calls: list[tuple[uuid.UUID, str, str]] = []
    assert await is_trusted(
        "MEMBER",
        login="x",
        repo="acme/app",
        workspace_id=uuid.uuid4(),
        permission_of=_lookup("read", calls),
    )
    assert calls == []


# ── both routes use it ───────────────────────────────────────────────────────


async def test_the_founders_issue_edit_reaches_the_run_when_github_says_contributor() -> None:
    calls: list[tuple[uuid.UUID, str, str]] = []
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING)
        async with sf() as session:
            routed = await route_issue_edit_to_run(
                session,
                workspace_id=ws,
                payload=edits._event(edits._BODY_V2, association="CONTRIBUTOR"),
                permission_of=_lookup("admin", calls),
            )
            await session.commit()
        run = await edits._run(sf, ws, run_id)

    assert routed == run_id
    assert "항상 저장한다" in run.payload["intent_text"]
    assert calls == [(ws, edits.REPO, "blasin")]


async def test_a_read_only_contributors_issue_edit_is_still_not_routed() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, _run_id = await edits._seed(sf, status=RunStatus.RUNNING)
        async with sf() as session:
            routed = await route_issue_edit_to_run(
                session,
                workspace_id=ws,
                payload=edits._event(edits._BODY_V2, association="CONTRIBUTOR"),
                permission_of=_lookup("read"),
            )

    assert routed is None


async def test_the_founders_pr_comment_reaches_the_run_when_github_says_contributor() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await comments._seed(sf, run_status=RunStatus.RUNNING)
        async with sf() as session:
            routed = await route_pr_comment_to_run(
                session,
                workspace_id=ws,
                payload=comments._payload(association="CONTRIBUTOR"),
                permission_of=_lookup("write"),
            )
            await session.commit()
        run = await comments._run(sf, run_id, ws)

    assert routed == run_id
    assert [c["body"] for c in run.payload["pr_comments"]] == ["fix the lint"]


async def test_a_read_only_contributors_pr_comment_is_still_not_routed() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        _run_id, ws = await comments._seed(sf, run_status=RunStatus.RUNNING)
        async with sf() as session:
            routed = await route_pr_comment_to_run(
                session,
                workspace_id=ws,
                payload=comments._payload(association="CONTRIBUTOR"),
                permission_of=_lookup("read"),
            )

    assert routed is None


@pytest.fixture
async def _pr_engine():  # noqa: ANN202 — the engine ``comments._worker_scene`` reads
    async with db_engine(Base) as (engine, _pg):
        comments._ENGINE[:] = [engine]
        yield
        comments._ENGINE.clear()


# ── the worker passes its lookup to the routes ───────────────────────────────


async def test_the_intake_worker_hands_its_permission_lookup_to_the_routes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Through the real ``IntakeWorker.drain_once``: the lookup it was built with reaches the
    routes, and the founder's CONTRIBUTOR edit lands on the run instead of a new request."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from backend.connectors.db import ConnectorAccountRow
    from backend.data.rls import workspace_session_scope
    from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository
    from backend.identity.workspaces_db import ProductRow, WorkspaceRow
    from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
    from backend.workflow.infrastructure.workers.intake_worker import (
        IntakeWorker,
        IntakeWorkerConfig,
    )

    calls: list[tuple[uuid.UUID, str, str]] = []
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, product_id, account_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(WorkspaceRow(id=ws, name="ws", safe_mode=True))
            await s.flush()
            s.add(ProductRow(id=product_id, workspace_id=ws, name="App", slug="app"))
            s.add(
                ConnectorAccountRow(
                    id=account_id,
                    workspace_id=ws,
                    connector="github",
                    webhook_token=f"tok-{uuid.uuid4().hex}",
                    signing_secret_ciphertext="cipher",
                )
            )
            await s.flush()
            await SqlAlchemyResourceBindingRepository(s).create(
                workspace_id=ws,
                product_id=product_id,
                connector_account_id=account_id,
                resource_id=edits.REPO,
                trigger={"filters": {}},
            )
            await s.commit()
        _ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING, workspace_id=ws)
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(
                TriggerEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    source="github",
                    trigger_kind=TriggerKind.WEBHOOK,
                    idempotency_key=f"k-{uuid.uuid4().hex}",
                    payload={
                        **edits._event(edits._BODY_V2, association="CONTRIBUTOR"),
                        "connector_account_id": str(account_id),
                        "resource_id": edits.REPO,
                    },
                    received_at=datetime.now(UTC),
                )
            )
            await s.commit()
        await IntakeWorker(
            session_factory=sf,
            config=IntakeWorkerConfig(),
            repo_permission=_lookup("admin", calls),
        ).drain_once()
        async with sf() as s, workspace_session_scope(s, ws):
            requests = list((await s.execute(select(RequestRow))).scalars().all())
        run = await edits._run(sf, ws, run_id)

    assert calls  # asked GitHub, since the association alone said CONTRIBUTOR
    assert len(requests) == 1  # only the run's own: the edit did not become a second run
    assert "항상 저장한다" in run.payload["intent_text"]


@pytest.mark.usefixtures("_pr_engine")
async def test_the_intake_worker_routes_the_founders_contributor_pr_comment() -> None:
    run, requests = await comments._worker_scene(
        filters={"action": "created"}, association="CONTRIBUTOR", repo_permission=_lookup("admin")
    )
    assert run.status is RunStatus.OPEN
    assert [c["body"] for c in run.payload["pr_comments"]] == ["fix the lint"]
    assert requests == []


async def test_the_runtime_wires_a_permission_lookup_into_the_intake_worker() -> None:
    """``build_worker_runtime`` is what prod runs; its IntakeWorker must carry a lookup."""
    from unittest.mock import MagicMock

    from backend.workflow.application.runtime import build_worker_runtime

    runtime = build_worker_runtime(
        session_factory=MagicMock(),
        execution=MagicMock(),
        delivery_adapter=MagicMock(),
        notify_sender=MagicMock(),
    )
    [intake] = [w for w in runtime.workers if getattr(w, "_name", None) == "intake_worker"]
    assert callable(intake._repo_permission)  # type: ignore[attr-defined]


# ── the github call ──────────────────────────────────────────────────────────


async def test_the_client_reads_the_collaborator_permission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/collaborators/blas1n/permission":
            return httpx.Response(200, json={"permission": "admin", "role_name": "admin"})
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GithubClient("t", client=http)
        assert await client.repo_permission("acme", "app", "blas1n") == "admin"
        assert await client.repo_permission("acme", "app", "stranger") is None


async def test_the_client_reports_maintain_from_role_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"permission": "write", "role_name": "maintain"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await GithubClient("t", client=http).repo_permission("a", "b", "c") == "maintain"


# ── the production lookup ────────────────────────────────────────────────────


async def test_the_lookup_asks_github_about_the_events_repo_with_the_bindings_token(
    monkeypatch,  # noqa: ANN001
) -> None:
    from types import SimpleNamespace

    import backend.workflow.application.runtime.intake_trust_runtime as rt

    asked: list[tuple[str, str, str, str]] = []

    class _Client:
        def __init__(self, token: str, *, base_url: str) -> None:
            self.token = token

        async def repo_permission(self, owner: str, repo: str, login: str) -> str:
            asked.append((self.token, owner, repo, login))
            return "admin"

    async def binding(session, *, workspace_id):  # noqa: ANN001, ANN202
        return SimpleNamespace(account=SimpleNamespace(delivery_config={}))

    async def creds(session, *, account, cipher):  # noqa: ANN001, ANN202
        return {"token": "tok"}

    monkeypatch.setattr(rt, "GithubClient", _Client)
    monkeypatch.setattr(rt, "resolve_github_binding", binding)
    monkeypatch.setattr(rt, "resolve_connector_credentials", creds)
    async with db_engine(Base) as (engine, _pg):
        lookup = rt.build_repo_permission_lookup(
            session_factory=async_sessionmaker(engine),
            cipher=object(),  # type: ignore[arg-type]
        )
        assert await lookup(uuid.uuid4(), "BSVibe/bsvibe-app", "blas1n") == "admin"
        assert await lookup(uuid.uuid4(), "not-a-repo", "blas1n") is None

    assert asked == [("tok", "BSVibe", "bsvibe-app", "blas1n")]


async def test_without_a_github_binding_the_lookup_says_none(monkeypatch) -> None:  # noqa: ANN001
    import backend.workflow.application.runtime.intake_trust_runtime as rt

    async def no_binding(session, *, workspace_id):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(rt, "resolve_github_binding", no_binding)
    async with db_engine(Base) as (engine, _pg):
        lookup = rt.build_repo_permission_lookup(
            session_factory=async_sessionmaker(engine),
            cipher=object(),  # type: ignore[arg-type]
        )
        assert await lookup(uuid.uuid4(), "a/b", "c") is None
