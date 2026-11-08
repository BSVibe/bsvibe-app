"""The PR body carries the independent review of the run's last PASSED round.

The real ``_open_pr_and_settle`` (the half a sandbox run and a ``client_attach`` run share),
reading a stored ``VerificationResult``; only the GitHub plugin call is a recording stub.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.delivery.connector_dispatch import _github_pr
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio

P, F = VerificationOutcome.PASSED, VerificationOutcome.FAILED
FINDING = {
    "severity": "high",
    "file": "src/a.py",
    "line": 12,
    "claim": "totals double count",
    "failure_scenario": "two items with one id -> total is doubled",
}


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Runner:
    def __init__(self) -> None:
        self.body = ""

    async def dispatch_action(self, plugin: Any, *, action_name: str, kwargs: Any, **_: Any) -> Any:
        if action_name == "open_pr":
            self.body = kwargs["body"]
            return {"url": "https://github.com/o/r/pull/9", "pr_number": 9}
        return {}


async def _seed_run(
    sf_: Any, rounds: list[tuple[VerificationOutcome, dict[str, Any]]]
) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(WorkspaceRow(id=workspace_id, name="ws"))
        await s.flush()
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                payload={},
            )
        )
        await s.flush()
        for outcome, result in rounds:
            s.add(
                VerificationResult(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    outcome=outcome,
                    contract={},
                    result=result,
                )
            )
            await s.flush()  # insertion order == created_at order
        await s.commit()
    return workspace_id, run_id


async def _pr_body(
    sf_: Any, workspace_id: uuid.UUID, run_id: uuid.UUID, source_issue: int | None = None
) -> str:
    runner = _Runner()
    deps = SimpleNamespace(
        session_factory=sf_, plugins_by_name={"github": object()}, runner=runner, settings=None
    )
    binding = SimpleNamespace(
        repo="o/r", base_branch="main", account=SimpleNamespace(delivery_config={})
    )
    await _github_pr._open_pr_and_settle(
        deps=deps,  # type: ignore[arg-type]
        binding=binding,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        deliverable_id=uuid.uuid4(),
        run_id=run_id,
        branch="b",
        title="t",
        body="Report",
        token="tok",
        source_issue=source_issue,
        action_prefix="github:outbound:code",
        pushed_by="server",
    )
    return runner.body


async def test_done_review_lists_each_finding_after_the_ledger(sf) -> None:
    ledger = [{"id": "R1", "text": "Do it", "kind": "do", "status": "done"}]
    other = {**FINDING, "severity": "low", "file": "b.py", "line": None, "claim": "naming"}
    workspace_id, run_id = await _seed_run(
        sf,
        [
            (P, {"review": {"status": "done", "findings": [other]}}),
            (P, {"requirements": ledger, "review": {"status": "done", "findings": [FINDING]}}),
            (F, {"review": {"status": "unavailable", "reason": "ignored: failed round"}}),
        ],
    )

    body = await _pr_body(sf, workspace_id, run_id)

    assert body.index("## Requirements ledger") < body.index("## Independent review")
    assert body.endswith(
        "## Independent review\n\n"
        "- **high** `src/a.py:12` — totals double count"
        " — two items with one id -> total is doubled"
    )
    assert "naming" not in body


async def test_multiline_finding_stays_one_line(sf) -> None:
    finding = {
        **FINDING,
        "claim": "# big\n\n- claim   spread",
        "failure_scenario": "줄1\n## 가짜 제목\n- 목록",
    }
    workspace_id, run_id = await _seed_run(
        sf, [(P, {"review": {"status": "done", "findings": [finding]}})]
    )

    body = await _pr_body(sf, workspace_id, run_id, source_issue=7)

    section = body.split("## Independent review\n\n", 1)[1]
    listing, _, tail = section.partition("\n\n")
    assert len(listing.splitlines()) == 1 and listing.startswith("- **high**")
    assert "\n## 가짜 제목" not in body
    assert "줄1 ## 가짜 제목 - 목록" in listing
    assert tail.strip() in ("Closes #7", "Refs #7")


async def test_multiline_file_stays_one_line(sf) -> None:
    finding = {**FINDING, "file": "a.py\n## x", "line": 5}
    workspace_id, run_id = await _seed_run(
        sf, [(P, {"review": {"status": "done", "findings": [finding]}})]
    )

    body = await _pr_body(sf, workspace_id, run_id)

    listing = body.split("## Independent review\n\n", 1)[1]
    assert len(listing.splitlines()) == 1 and listing.startswith("- **high**")
    assert "\n## x" not in body


async def test_long_field_is_cut_at_300_chars(sf) -> None:
    workspace_id, run_id = await _seed_run(
        sf, [(P, {"review": {"status": "done", "findings": [{**FINDING, "claim": "x" * 400}]}})]
    )

    body = await _pr_body(sf, workspace_id, run_id)

    assert "x" * 300 + "…" in body
    assert "x" * 301 not in body


async def test_clean_review_says_no_findings(sf) -> None:
    workspace_id, run_id = await _seed_run(
        sf, [(P, {"review": {"status": "done", "findings": []}})]
    )

    body = await _pr_body(sf, workspace_id, run_id)

    assert body.endswith("## Independent review\n\nNo findings.")


async def test_unavailable_review_gives_the_reason(sf) -> None:
    workspace_id, run_id = await _seed_run(
        sf, [(P, {"review": {"status": "unavailable", "reason": "review timed out"}})]
    )

    body = await _pr_body(sf, workspace_id, run_id)

    assert body.endswith("## Independent review\n\nUnavailable: review timed out")


async def test_no_review_no_section(sf) -> None:
    workspace_id, run_id = await _seed_run(sf, [(P, {"requirements": []})])

    body = await _pr_body(sf, workspace_id, run_id)

    assert body == "Report"
