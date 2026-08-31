"""요청 원본은 런이 아니라 **Request** 에 붙는다 (intake).

처음엔 settle 싱크에서 ``Settlement.intent_text`` 로 요청을 남겼다. prod 재측정이
그 설계를 두 번 뒤집었다:

======================================  ==========================================
가정                                     실측 (2026-08-31, prod)
======================================  ==========================================
"정착 안 되는 런은 소수"                 런 231건 중 **116건(정확히 절반)이 정착 안 됨**
"런당 요청 하나이므로 키는 run_id"       request 13개가 런을 2~3개씩 낳는다 →
                                        같은 지시문이 **25개 파일로 중복**된다
"요청 원문은 런 payload 에 있다"         런 19건엔 없다. ``requests.payload`` 의
                                        ``text``(213건)가 더 완전한 원본이다
======================================  ==========================================

그래서 기록 지점은 **Request 가 만들어지는 순간**이고 키는 ``request_id`` 다.
런이 정착하든, 실패하든, 아예 시작을 못 하든 형님이 쓴 지시문은 남는다 — 그게
"히스토리성"의 뜻이다.

책임 분리: intake 는 **요청**(런 시작 전), settle 싱크는 **피드백·회고**(런 안에서
발생). 한 원본은 한 곳에서만 기록된다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.workflow.infrastructure.intake.db import TriggerEventRow, TriggerKind
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

from .._support import db_engine

INTENT = (
    "라우팅 규칙이 몇 개인지 한 문장으로만 답해줘.\n조사만 하고 보고해라 — 파일은 하나도 쓰지 마라."
)


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _requests_dir(vault_root: Path, workspace_id: uuid.UUID) -> Path:
    """``seeds/request/`` for one workspace.

    The middle segment is a DEPLOYMENT constant, read inline rather than bound
    to a local of that name (``test_the_workspace_region_axis_is_gone``).
    """
    return (
        vault_root
        / get_settings().knowledge_default_region
        / str(workspace_id)
        / "seeds"
        / "request"
    )


async def _seed_trigger(
    sf, *, workspace_id: uuid.UUID, kind: TriggerKind, payload: dict
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            TriggerEventRow(
                id=trigger_id,
                workspace_id=workspace_id,
                trigger_kind=kind,
                source="test",
                idempotency_key=f"k-{trigger_id}",
                payload=payload,
                received_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return trigger_id


def _worker(sf, tmp_path: Path) -> IntakeWorker:
    settings = get_settings().model_copy(update={"knowledge_vault_root": str(tmp_path)})
    return IntakeWorker(session_factory=sf, settings=settings)


class TestTheRequestOriginalIsRecordedAtIntake:
    @pytest.mark.asyncio
    async def test_the_founders_words_are_recorded_verbatim(self, sf, tmp_path: Path) -> None:
        ws = uuid.uuid4()
        await _seed_trigger(sf, workspace_id=ws, kind=TriggerKind.DIRECT, payload={"text": INTENT})

        assert await _worker(sf, tmp_path).drain_once() == 1

        written = list(_requests_dir(tmp_path, ws).glob("*.md"))
        assert len(written) == 1
        assert INTENT in written[0].read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_it_is_recorded_before_any_run_exists(self, sf, tmp_path: Path) -> None:
        """정착은커녕 런조차 없는 시점에 이미 남는다.

        prod 런 231건 중 116건이 끝내 정착하지 않았다. 정착에 매달면 형님 지시문의
        절반이 사라진다.
        """
        ws = uuid.uuid4()
        await _seed_trigger(sf, workspace_id=ws, kind=TriggerKind.DIRECT, payload={"text": INTENT})

        await _worker(sf, tmp_path).drain_once()

        from sqlalchemy import func, select

        from backend.workflow.infrastructure.db import ExecutionRun

        async with sf() as s:
            runs = (await s.execute(select(func.count()).select_from(ExecutionRun))).scalar_one()
        assert runs == 0, "이 시점엔 런이 아직 없다"
        assert list(_requests_dir(tmp_path, ws).glob("*.md")), "그래도 요청 원본은 남아야 한다"

    @pytest.mark.asyncio
    async def test_intent_text_is_accepted_too(self, sf, tmp_path: Path) -> None:
        """``requests.payload`` 는 ``text`` 가 주류(213건)지만 ``intent_text`` 도 10건 있다."""
        ws = uuid.uuid4()
        await _seed_trigger(
            sf, workspace_id=ws, kind=TriggerKind.DIRECT, payload={"intent_text": INTENT}
        )

        await _worker(sf, tmp_path).drain_once()

        written = list(_requests_dir(tmp_path, ws).glob("*.md"))
        assert len(written) == 1
        assert INTENT in written[0].read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_trigger_without_founder_words_records_nothing(
        self, sf, tmp_path: Path
    ) -> None:
        """지시문이 없는 트리거(스케줄 tick 등)는 원본을 만들지 않는다 — 빈 파일이 쌓이면 안 된다."""
        ws = uuid.uuid4()
        await _seed_trigger(
            sf,
            workspace_id=ws,
            kind=TriggerKind.SCHEDULE,
            payload={"kind": "cron", "cron_expr": "30 0 * * 1"},
        )

        assert await _worker(sf, tmp_path).drain_once() == 1
        assert not list(_requests_dir(tmp_path, ws).glob("*.md"))

    @pytest.mark.asyncio
    async def test_recording_failure_does_not_stop_the_drain(self, sf, tmp_path: Path) -> None:
        """vault 가 못 쓰는 상태여도 Request 는 정상적으로 만들어져야 한다.

        원본 기록은 intake 의 부수 효과다. 이게 드레인을 멈추면 vault 문제가
        **일 자체를 못 들어오게** 막는다.
        """
        ws = uuid.uuid4()
        blocker = tmp_path / get_settings().knowledge_default_region
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("나는 디렉터리가 아니다", encoding="utf-8")
        await _seed_trigger(sf, workspace_id=ws, kind=TriggerKind.DIRECT, payload={"text": INTENT})

        assert await _worker(sf, tmp_path).drain_once() == 1

        from sqlalchemy import func, select

        from backend.workflow.infrastructure.intake.db import RequestRow

        async with sf() as s:
            made = (await s.execute(select(func.count()).select_from(RequestRow))).scalar_one()
        assert made == 1, "vault 사고가 Request 생성을 막으면 안 된다"


class TestOneOriginalPerRequest:
    @pytest.mark.asyncio
    async def test_the_filename_is_the_request_id(self, sf, tmp_path: Path) -> None:
        """파일명이 ``request_id`` 여야 한다 — 이게 중복을 막는 유일한 장치다.

        타임스탬프로 이름 지으면 같은 요청이 여러 파일이 된다. prod 실측에서
        request 13개가 런을 2~3개씩 낳았으므로 ``run_id`` 로 잡아도 마찬가지다.

        ⚠️ 재드레인으로는 이걸 못 잡는다 — 두 번째 드레인은 이미 Request 가 있는
        트리거를 아예 집지 않으므로, 키가 무엇이든 파일은 하나로 남는다(알리바이).
        키 선택을 직접 고정해야 한다.
        """
        from sqlalchemy import select

        from backend.workflow.infrastructure.intake.db import RequestRow

        ws = uuid.uuid4()
        await _seed_trigger(sf, workspace_id=ws, kind=TriggerKind.DIRECT, payload={"text": INTENT})

        await _worker(sf, tmp_path).drain_once()

        async with sf() as s:
            request_id = (await s.execute(select(RequestRow.id))).scalar_one()

        written = list(_requests_dir(tmp_path, ws).glob("*.md"))
        assert len(written) == 1
        assert written[0].stem == str(request_id)

    @pytest.mark.asyncio
    async def test_two_runs_from_one_request_share_one_original(self, sf, tmp_path: Path) -> None:
        """같은 Request 로 두 번 기록해도 원본은 하나, 그리고 처음 바이트가 이긴다.

        ``record_original`` 의 ``O_EXCL`` 불변성이 여기서 쓰인다 — 백필과 실시간
        기록이 같은 요청을 건드려도 과거가 다시 쓰이지 않는다.
        """
        from backend.knowledge.factory import KnowledgeFactory
        from backend.knowledge.originals import record_original

        ws = uuid.uuid4()
        request_id = uuid.uuid4()
        vault = KnowledgeFactory(workspace_id=str(ws), vault_root=tmp_path).vault()

        for content in (INTENT, "나중에 덮어쓰려는 것"):
            await record_original(
                vault=vault,
                kind="request",
                key=str(request_id),
                title="t",
                content=content,
            )

        written = list(_requests_dir(tmp_path, ws).glob("*.md"))
        assert len(written) == 1
        body = written[0].read_text(encoding="utf-8")
        assert INTENT in body
        assert "나중에 덮어쓰려는 것" not in body
