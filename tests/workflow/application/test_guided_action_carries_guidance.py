"""*"지침 주고 다시 시도"* 버튼이 실제로 지침을 나른다 (§14).

`_checkpoint_shared.ACTION_RETRY` 의 라벨은 **"지침 주고 다시 시도" / "Guide &
retry"** 이고, 그 액션 표의 주석은 ``retry`` 가 *"re-opens the run **with the
founder's guidance**"* 라고 적어뒀다. 그런데 같은 레포 안 네 곳이 서로 모순했다:

===========================================  =================================
위치                                          말하는 것
===========================================  =================================
``_checkpoint_shared.py:146``                 ``retry`` = *"with the founder's guidance"*
버튼 라벨                                      **"지침 주고 다시 시도"**
``api/v1/checkpoints.py`` ``reason`` 주석      *"ignored for non-discard resolutions"*
``CheckpointRow.tsx`` ``submitAction``        ``{ action_key }`` 만 전송
===========================================  =================================

실제 결과 — 재개된 에이전트가 받는 첫 메시지는

    "The founder resolved a prior question — Q: … **A: retry**. Continue …"

즉 **지침 자리에 버튼 키 문자열**이 들어간다. 형님이 PWA 에서 친 글자는 요청에
실리지도 않았다(클라이언트에서 버려짐) — L210 의 ``resolution_text = action_key
if action_key is not None else answer`` 는 두 번째 방어선일 뿐이었다.

여기서 잇는 링크 하나: **액션 버튼이 형님이 친 글자를 함께 보내고, 그 글자가
재개 메시지가 된다.** 없는 기계를 만드는 게 아니다 — 자유 텍스트 재개 경로는
이미 정상 동작한다.

⚠️ 불변식 (이 파일이 지킨다)

1. ``decision.resolution`` 과 감사 이벤트의 ``answer`` 는 **계속 액션 키**다 —
   로케일 독립 wire 식별자(L206~209). 바뀌는 것은 *재개 메시지가 무엇을
   나르는가*지 *무엇이 기록되는가*가 아니다.
2. **#823 과의 상호작용** — ``founder_authored_text`` 는 ``action_key`` 가 있으면
   ``reason`` 이 비어 있는 한 ``None`` 을 낸다. 지침을 그 술어가 못 보는 곳으로
   흘리면 **어제 머지한 게이트가 오늘의 정당한 지식을 조용히 억제**한다.
3. 글자 없는 액션(``acknowledge`` · 사유 없는 ``discard``)은 **계속 억제**된다.
4. 사유 있는 폐기는 **계속 한 장**(negative_pattern)만 남는다 — 형님의 문장은
   그 행이 이미 나르므로 decision_resolution 행은 나르지 않는다. trust
   ratchet(#759/#760)의 심장을 과교정으로 죽이면 안 된다.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.checkpoints import ResolveRequest
from backend.common.settle_kinds import (
    DECISION_RESOLUTION_SETTLE_KIND,
    NEGATIVE_PATTERN_SETTLE_KIND,
    founder_authored_text,
)
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workflow.application._checkpoint_shared import (
    ACTION_DISCARD,
    ACTION_RETRY,
)
from backend.workflow.application._loop_context import _resumption_messages
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from plugin.audit.models import AuditOutboxRecord

from ..._support import db_engine, fake_current_user

_ROOT = Path(__file__).resolve().parents[3]
_PWA_CHECKPOINTS_TS = _ROOT / "apps" / "pwa" / "lib" / "api" / "checkpoints.ts"

#: 형님이 액션 버튼과 함께 친 지침. 액션 키(``retry``)와 **한 글자도 겹치지 않게**
#: 골랐다 — 재개 메시지에서 둘을 확실히 구분하려면 그래야 한다.
GUIDANCE = "충돌난 두 쪽 다 살리지 말고 우리 브랜치 쪽 계산식을 남겨라"


# ── 1. 주는 쪽 — PWA 가 실제로 무엇을 싣는가 ─────────────────────────────────


def _resolve_action_fn_source() -> str:
    """``resolveCheckpointAction`` 함수 본문 소스.

    파이썬에서 TS 를 읽는 이유: 이 사슬의 재발 결함은 *양끝이 다 green 인데
    가운데 선이 끊긴 것*이다. 주는 쪽을 하드코딩한 dict 로 흉내내면 PWA 가
    지침을 다시 버려도 이 파일은 계속 green 이다 — seam 이 아니게 된다.
    """
    src = _PWA_CHECKPOINTS_TS.read_text(encoding="utf-8")
    match = re.search(r"export function resolveCheckpointAction\(.*?\n\}", src, flags=re.DOTALL)
    assert match is not None, "resolveCheckpointAction 을 PWA 소스에서 못 찾았다"
    return match.group(0)


def test_the_pwa_action_request_carries_the_founders_words() -> None:
    """주는 쪽. 액션 버튼 요청 본문에 형님이 친 글자가 실린다."""
    source = _resolve_action_fn_source()
    assert "action_key" in source
    assert "reason" in source, (
        "액션 버튼이 형님이 친 지침을 요청에 싣지 않는다 — 버튼 라벨이 "
        "'지침 주고 다시 시도'인데 지침이 흐를 관이 없다"
    )


# ── 2. 받는 쪽 REST 모델 — extra=forbid 를 통과하는가 ────────────────────────


def test_the_rest_model_accepts_the_pwa_action_body() -> None:
    """PWA 가 보내는 바로 그 본문이 ``extra="forbid"`` 모델을 통과한다.

    ``extra="forbid"`` 이므로 백엔드가 필드를 지우면 여기서 422 로 잡힌다 —
    받는 쪽을 끊는 것도 이 seam 안에서 빨개진다."""
    parsed = ResolveRequest.model_validate({"action_key": ACTION_RETRY, "reason": GUIDANCE})
    assert parsed.action_key == ACTION_RETRY
    assert parsed.reason == GUIDANCE


# ── 3. 뒤끝 — 지침이 재개 메시지가 된다 ──────────────────────────────────────


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_merge_conflict_review(sf, workspace_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """RUNNING 런 + 그 위의 PENDING ``merge_conflict_review`` Decision."""
    run_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": "리포트에 기간 표시 추가"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=workspace_id,
                decision="merge_conflict_review",
                payload={"reason": "ambiguous_conflict"},
                status=DecisionStatus.PENDING,
            )
        )
        await s.commit()
    return run_id, decision_id


async def _run_payload(sf, run_id: uuid.UUID) -> dict:
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        return dict(run.payload or {})


async def _settle_payloads(sf, kind: str) -> list[dict]:
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .all()
        )
    return [r.payload for r in rows if (r.payload or {}).get("kind") == kind]


def _notes(vault_root: Path, workspace_id: uuid.UUID, region: str = "us-1") -> list[Path]:
    ws_dir = vault_root / region / str(workspace_id)
    return list(ws_dir.rglob("*.md")) if ws_dir.exists() else []


async def _drain(sf, vault_root: Path) -> int:
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=vault_root),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    return await worker.drain_once()


@pytest_asyncio.fixture
async def client(sf, workspace_id: uuid.UUID, founder_id: uuid.UUID):
    """진짜 REST 표면. seam 이 층을 건너뛰지 않도록 서비스를 직접 부르지 않는다."""
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _resolve_via_pwa_action_body(
    client: httpx.AsyncClient, decision_id: uuid.UUID, body: dict
) -> dict:
    """PWA 요청 본문 → HTTP → REST 모델 → 서비스. 층을 건너뛰지 않는다."""
    r = await client.post(f"/api/v1/checkpoints/{decision_id}/resolve", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_guided_retry_seeds_the_founders_words_not_the_action_key(
    sf, client, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """**seam 전체** — PWA 요청 모양 → REST 모델 → ``resolve_checkpoint`` →
    ``run.payload["resolved_decisions"]`` → ``_resumption_messages`` 출력.

    재개하는 에이전트가 읽는 문장에 형님의 지침이 있고, 벌거벗은 액션 키는 없다.
    """
    run_id, decision_id = await _seed_merge_conflict_review(sf, workspace_id)

    await _resolve_via_pwa_action_body(
        client, decision_id, {"action_key": ACTION_RETRY, "reason": GUIDANCE}
    )

    payload = await _run_payload(sf, run_id)
    resolved = payload["resolved_decisions"]
    assert len(resolved) == 1
    assert resolved[0]["answer"] == GUIDANCE

    run = ExecutionRun(
        id=run_id,
        workspace_id=workspace_id,
        status=RunStatus.OPEN,
        payload=payload,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    messages = _resumption_messages(run)
    assert len(messages) == 1
    content = messages[0]["content"]
    assert GUIDANCE in content, content
    assert f"A: {ACTION_RETRY}" not in content, content


async def test_a_guided_retry_still_records_the_action_key_as_the_resolution(
    sf, client, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """불변식 1 — 기록되는 것은 계속 액션 키다(로케일 독립 wire 식별자).
    감사 이벤트도 마찬가지. 바뀌는 것은 재개 메시지뿐이다."""
    _run_id, decision_id = await _seed_merge_conflict_review(sf, workspace_id)

    outcome = await _resolve_via_pwa_action_body(
        client, decision_id, {"action_key": ACTION_RETRY, "reason": GUIDANCE}
    )
    assert outcome["resolution"] == ACTION_RETRY

    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None
        assert decision.resolution == ACTION_RETRY
        assert decision.status is DecisionStatus.RESOLVED

        rows = (await s.execute(select(AuditOutboxRecord))).scalars().all()
    events = [r.payload for r in rows if r.payload["data"].get("decision_id") == str(decision_id)]
    assert len(events) == 1
    assert events[0]["data"]["answer"] == ACTION_RETRY
    assert events[0]["data"]["action_key"] == ACTION_RETRY


async def test_a_guided_retry_reopens_the_run(
    sf, client, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """``retry`` 는 전용 핸들러 없이 재개 분기로 떨어진다 — RUNNING → OPEN."""
    run_id, decision_id = await _seed_merge_conflict_review(sf, workspace_id)

    await _resolve_via_pwa_action_body(
        client, decision_id, {"action_key": ACTION_RETRY, "reason": GUIDANCE}
    )

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.OPEN


# ── 4. #823 상호작용 — 오늘의 게이트가 내일의 지식을 죽이지 않는다 ───────────


def test_the_shared_leaf_rule_sees_the_guidance() -> None:
    """지침은 ``founder_authored_text`` 가 **보는 자리**에 있어야 한다.
    ``action_key`` 만 보고 판정하면 #823 이 정당한 지식을 조용히 억제한다."""
    assert (
        founder_authored_text(answer=ACTION_RETRY, reason=GUIDANCE, action_key=ACTION_RETRY)
        == GUIDANCE
    )
    # 글자 없는 액션은 계속 억제된다 — 규칙이 느슨해진 게 아니다.
    assert founder_authored_text(answer=ACTION_RETRY, reason="", action_key=ACTION_RETRY) is None


async def test_a_guided_retry_becomes_knowledge(
    sf, client, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """#823 상호작용의 핵심 — 지침 있는 ``retry`` 는 **노트가 된다.**
    (지침을 게이트가 못 보는 곳으로 흘리면 이 테스트가 빨개진다.)"""
    _run_id, decision_id = await _seed_merge_conflict_review(sf, workspace_id)

    await _resolve_via_pwa_action_body(
        client, decision_id, {"action_key": ACTION_RETRY, "reason": GUIDANCE}
    )

    payloads = await _settle_payloads(sf, DECISION_RESOLUTION_SETTLE_KIND)
    assert len(payloads) == 1
    # 감사 흔적은 그대로 — settle 의 ``answer`` 는 계속 액션 키다(#823 의 보증).
    assert payloads[0]["answer"] == ACTION_RETRY
    assert payloads[0]["action_key"] == ACTION_RETRY
    # 지침은 재시도이지 거절이 아니다 — negative_pattern 행은 생기지 않는다.
    assert await _settle_payloads(sf, NEGATIVE_PATTERN_SETTLE_KIND) == []

    assert await _drain(sf, tmp_path) == 1
    notes = _notes(tmp_path, workspace_id)
    assert len(notes) == 1, "지침 있는 retry 가 지식이 되지 못했다 — #823 이 억제했다"
    body = notes[0].read_text(encoding="utf-8")
    assert GUIDANCE in body


async def test_a_text_free_retry_still_earns_no_note(
    sf, client, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """양성 대조군 — 지침 없이 누른 ``retry`` 는 형님이 친 글자가 0자다.
    #823 의 억제가 그대로 유지된다(과교정 아님)."""
    _run_id, decision_id = await _seed_merge_conflict_review(sf, workspace_id)

    await _resolve_via_pwa_action_body(client, decision_id, {"action_key": ACTION_RETRY})

    assert len(await _settle_payloads(sf, DECISION_RESOLUTION_SETTLE_KIND)) == 1
    assert await _drain(sf, tmp_path) == 1
    assert _notes(tmp_path, workspace_id) == []


async def test_a_pwa_discard_with_a_reason_still_keeps_exactly_one_note(
    sf, client, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """양성 대조군 — 사유 있는 폐기는 **한 장**(negative_pattern)만 남는다.

    형님의 문장은 그 행이 이미 나른다. 같은 사건에 decision_resolution 노트를
    또 만들면 #823 이 못 박아둔 *"한 장"* 을 깨는 것이다. 덤으로 이 경로가
    이제 **PWA 에서도 도달 가능**하다 — §14.2 실측에서 PWA 는 ``reason`` 을
    보낸 적이 없어 negative_pattern 19건이 전부 MCP/REST 발이었다.
    """
    _run_id, decision_id = await _seed_merge_conflict_review(sf, workspace_id)
    reason = "이 접근은 실제 스택 위에서 깨지는 것을 정의상 못 잡는다"

    await _resolve_via_pwa_action_body(
        client, decision_id, {"action_key": ACTION_DISCARD, "reason": reason}
    )

    assert len(await _settle_payloads(sf, DECISION_RESOLUTION_SETTLE_KIND)) == 1
    assert len(await _settle_payloads(sf, NEGATIVE_PATTERN_SETTLE_KIND)) == 1

    assert await _drain(sf, tmp_path) == 2
    notes = _notes(tmp_path, workspace_id)
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    assert reason in body
    assert f"A: {ACTION_DISCARD}" not in body
