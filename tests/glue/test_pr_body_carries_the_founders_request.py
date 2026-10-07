"""#1144 — a PR BSVibe opens says what was asked, and links what it names.

Measured 2026-10-07, PR #1142 (#1073's fix, from a Direct run). Its whole body:

    라우팅 계정 누락 시 런 멈춤 수정
    바뀐 파일 6개: …
    검증: 8개 확인 통과. 결과 시연됨 (2개 프로브).

The directive said "GitHub 이슈 #1073 을 고쳐줘" and spelled out the cause and the
acceptance criteria — none of it reached the reviewer, and nothing linked #1073.
(An issue-SOURCED run already gets ``Closes #N``; a Direct run has no issue on
its trigger.) The agent's own prose is deliberately kept out of the body — it is
streaming narration — but the FOUNDER's directive is the why.

형님 ruled (2026-10-07): the body carries the directive verbatim, and every
``#N`` it names is linked with a NON-closing ``Refs`` — a directive that says
"이전 런은 … #1141 로 고쳤다" must not close #1141 on merge. Closing stays with
issue-sourced runs.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.connector_dispatch import (
    build_connector_delivery_adapter,
)
from backend.workflow.application.delivery.connector_dispatch._github_pr import (
    with_founder_request,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
)
from tests.glue.test_github_delivery_e2e import (  # noqa: F401 — fixtures
    GITHUB_API,
    _execution_deps,
    _make_bare_remote,
    _plugins,
    _scripted_writes_file,
    _seed_github_connector,
    cipher,
    sf,
)

pytestmark = pytest.mark.asyncio

_DIRECTIVE = (
    "(재시도 — 이전 런 4414bcd5 은 검증 게이트 결함으로 폐기했고, 그 결함은 #1141 로 고쳤다.)\n\n"
    "GitHub 이슈 #1073 을 고쳐줘 — 라우팅 규칙의 대상 계정이 없으면 기본 규칙으로 넘어가지 않는다.\n\n"
    "## 수용 기준\n- 대상 계정이 없는 규칙은 건너뛴다"
)
_BODY = "라우팅 계정 누락 시 런 멈춤 수정\n\n바뀐 파일 2개:\n- a.py\n- b.py"


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------


def test_the_directive_is_quoted_verbatim_under_the_body() -> None:
    out = with_founder_request(_BODY, intent=_DIRECTIVE, source_issue=None, language="ko")

    assert out.startswith(_BODY)
    assert "> GitHub 이슈 #1073 을 고쳐줘" in out
    assert "> ## 수용 기준" in out  # quoted, so its headings do not outrank the body


def test_every_named_issue_is_referenced_and_none_is_closed() -> None:
    out = with_founder_request(_BODY, intent=_DIRECTIVE, source_issue=None, language="ko")

    assert "Refs #1141, #1073" in out
    assert "Closes" not in out


def test_a_number_named_twice_is_referenced_once() -> None:
    out = with_founder_request(
        _BODY, intent="#12 를 보고 #12 처럼 고쳐", source_issue=None, language="en"
    )

    assert out.count("#12") == 3  # two inside the quote, one in Refs


def test_an_issue_sourced_run_is_left_as_it_was() -> None:
    """Its body already carries ``Closes #N`` and the issue IS the request."""
    assert with_founder_request(_BODY, intent=_DIRECTIVE, source_issue=42, language="ko") == _BODY


def test_no_directive_leaves_the_body_alone() -> None:
    assert with_founder_request(_BODY, intent="  ", source_issue=None, language="ko") == _BODY


# ---------------------------------------------------------------------------
# Through the real delivery path
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_direct_runs_pr_carries_the_request(
    sf: async_sessionmaker[AsyncSession],  # noqa: F811
    cipher: CredentialCipher,  # noqa: F811
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    bare = await _make_bare_remote(tmp_path)
    workspace_root = tmp_path / "runs"
    pr_route = respx.post(f"{GITHUB_API}/repos/owner/name/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": 7, "html_url": "https://github.com/owner/name/pull/7"}
        )
    )

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await _seed_github_connector(s, cipher, workspace_id)
        s.add(
            ExecutionRun(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                request_id=None,
                status=RunStatus.OPEN,
                payload={"intent_text": "이슈 #77 을 고쳐줘 — 기능을 더해"},
            )
        )
        await s.commit()

    deps = _execution_deps(sf, workspace_root, cipher, bare, _scripted_writes_file())
    assert await AgentWorker(session_factory=sf, execution=deps).drive_once() == 1
    adapter = build_connector_delivery_adapter(
        session_factory=sf,
        plugins=list((await _plugins()).values()),
        cipher=cipher,
        workspace_root=workspace_root,
        remote_url_for=lambda _repo: bare.as_uri(),
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    pr_body = json.loads(pr_route.calls.last.request.content)["body"]
    assert "> 이슈 #77 을 고쳐줘" in pr_body
    assert "Refs #77" in pr_body
    assert "Closes" not in pr_body
