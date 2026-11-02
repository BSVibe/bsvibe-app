"""A founder's checklist comment reaches the ledger even when GitHub calls them CONTRIBUTOR.

The ledger reads an ``issue_comment``'s comment only from a trusted author, judged by the
webhook's ``author_association``. On an org-owned repo a member whose membership is private is
reported as CONTRIBUTOR (#1279), so the founder's own checklist comments never became ledger
items. Intake — which can ask GitHub — now records the verdict on the request payload, and the
ledger reads that.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application.intake.trust import stamp_comment_trust
from backend.workflow.domain.requirements import COMMENT_TRUSTED_KEY, build_ledger
from plugin.github.webhook import parse_webhook
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

REPO = "acme/app"
_ISSUE = "## 할 것\n- [ ] 이슈에 적힌 항목\n"
_COMMENT = "추가로:\n- [ ] 댓글에 적힌 항목\n"


def _comment_event(association: str, login: str = "blas1n") -> dict[str, Any]:
    raw = json.dumps(
        {
            "action": "created",
            "issue": {"number": 7, "title": "t", "body": _ISSUE, "user": {"login": "blas1n"}},
            "comment": {
                "body": _COMMENT,
                "user": {"login": login, "type": "User"},
                "author_association": association,
            },
            "repository": {"full_name": REPO},
            "sender": {"login": login, "type": "User"},
        }
    ).encode()
    trig = parse_webhook(
        workspace_id=uuid.uuid4(),
        headers={"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": uuid.uuid4().hex},
        raw_body=raw,
    )
    assert trig is not None
    return dict(trig.payload)


def _lookup(answer: str | None, calls: list | None = None):  # type: ignore[no-untyped-def]
    async def permission_of(workspace_id: uuid.UUID, repo: str, login: str) -> str | None:
        if calls is not None:
            calls.append((repo, login))
        return answer

    return permission_of


def _texts(payload: dict[str, Any]) -> set[str]:
    return {i["text"] for i in build_ledger(payload)}


async def test_a_contributor_with_write_access_has_their_comment_items_ledgered() -> None:
    calls: list = []
    payload = await stamp_comment_trust(
        _comment_event("CONTRIBUTOR"),
        workspace_id=uuid.uuid4(),
        permission_of=_lookup("admin", calls),
    )
    assert payload[COMMENT_TRUSTED_KEY] is True
    assert _texts(payload) == {"이슈에 적힌 항목", "댓글에 적힌 항목"}
    assert calls == [(REPO, "blas1n")]


async def test_a_read_only_contributors_comment_stays_out_of_the_ledger() -> None:
    payload = await stamp_comment_trust(
        _comment_event("CONTRIBUTOR", login="rando"),
        workspace_id=uuid.uuid4(),
        permission_of=_lookup("read"),
    )
    assert COMMENT_TRUSTED_KEY not in payload
    assert _texts(payload) == {"이슈에 적힌 항목"}


async def test_without_a_verdict_a_contributor_comment_is_not_ledgered() -> None:
    assert _texts(_comment_event("CONTRIBUTOR")) == {"이슈에 적힌 항목"}
    assert _texts(_comment_event("OWNER")) == {"이슈에 적힌 항목", "댓글에 적힌 항목"}


async def test_other_events_and_trusted_associations_are_left_alone() -> None:
    calls: list = []
    owner = _comment_event("OWNER")
    assert (
        await stamp_comment_trust(
            owner, workspace_id=uuid.uuid4(), permission_of=_lookup("admin", calls)
        )
        == owner
    )
    other = {"github_event": "issues", "action": "opened", "repo": REPO}
    assert (
        await stamp_comment_trust(
            other, workspace_id=uuid.uuid4(), permission_of=_lookup("admin", calls)
        )
        == other
    )
    assert calls == []


async def test_the_intake_worker_stamps_the_verdict_on_the_request() -> None:
    from backend.connectors.db import ConnectorAccountRow
    from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository
    from backend.identity.workspaces_db import ProductRow, WorkspaceRow
    from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
    from backend.workflow.infrastructure.workers.intake_worker import (
        IntakeWorker,
        IntakeWorkerConfig,
    )

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
                resource_id=REPO,
                trigger={"filters": {}},
            )
            s.add(
                TriggerEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    source="github",
                    trigger_kind=TriggerKind.WEBHOOK,
                    idempotency_key=f"k-{uuid.uuid4().hex}",
                    payload={
                        **_comment_event("CONTRIBUTOR"),
                        "connector_account_id": str(account_id),
                        "resource_id": REPO,
                    },
                    received_at=datetime.now(UTC),
                )
            )
            await s.commit()
        await IntakeWorker(
            session_factory=sf, config=IntakeWorkerConfig(), repo_permission=_lookup("admin")
        ).drain_once()
        async with sf() as s, workspace_session_scope(s, ws):
            [request] = list((await s.execute(select(RequestRow))).scalars().all())

    assert "댓글에 적힌 항목" in _texts(request.payload)
