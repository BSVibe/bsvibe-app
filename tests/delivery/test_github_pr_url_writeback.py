"""deliver_github persists the opened PR's URL back to the Deliverable (#362).

`deliver_github` opens the PR via the github plugin's `open_pr` action (whose
result carries `url` = the PR's html_url) but historically never wrote that URL
onto `Deliverable.diff_url` — so the PWA/Brief couldn't surface the PR link
(every delivered code deliverable showed an empty diff_url even after a PR
opened, e.g. #366). The `_persist_pr_url` helper closes that gap.
"""

from __future__ import annotations

import uuid

from backend.workflow.application.delivery.connector_dispatch._github import _persist_pr_url
from backend.workflow.infrastructure.db import Deliverable, DeliverableType
from tests._support import shared_file_sessionmaker


async def _seed_deliverable(factory) -> uuid.UUID:  # noqa: ANN001
    did = uuid.uuid4()
    async with factory() as session:
        session.add(
            Deliverable(
                id=did,
                run_id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                deliverable_type=DeliverableType.CODE,
                artifact_uri=None,
                diff_url=None,
                payload={"summary": "s"},
            )
        )
        await session.commit()
    return did


async def test_persist_pr_url_sets_diff_url() -> None:
    async with shared_file_sessionmaker() as factory:
        did = await _seed_deliverable(factory)
        await _persist_pr_url(factory, did, "https://github.com/o/n/pull/7")
        async with factory() as session:
            d = await session.get(Deliverable, did)
            assert d is not None and d.diff_url == "https://github.com/o/n/pull/7"


async def test_persist_pr_url_noop_when_empty() -> None:
    async with shared_file_sessionmaker() as factory:
        did = await _seed_deliverable(factory)
        await _persist_pr_url(factory, did, None)
        await _persist_pr_url(factory, did, "")
        async with factory() as session:
            d = await session.get(Deliverable, did)
            assert d is not None and d.diff_url is None


async def test_persist_pr_url_missing_deliverable_noop() -> None:
    # A deliverable that doesn't exist must not raise (soft).
    async with shared_file_sessionmaker() as factory:
        await _persist_pr_url(factory, uuid.uuid4(), "https://github.com/o/n/pull/9")
