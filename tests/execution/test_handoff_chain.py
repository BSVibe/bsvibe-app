"""체이닝 — 프레임이 쪼갠 스텝을 따라 다음 런을 만든다 (PR B).

옛 규칙은 고정 2단계였다: 프레임이 ``design_then_impl`` 로 맞히면 설계 런
하나와 구현 런 하나. 이제 단계 수도 이름도 **형님의 라우팅 룰**에서 나오고,
프레임은 그 어휘로 작업을 쪼갠 ``steps`` 를 남긴다. 체이닝은 그 목록을 따라
걸을 뿐이다 — 판단하지 않는다.

각 스텝은 **자기 지시문**(``intent``)을 들고 간다. 하드코딩된 "명세만 쓰고
구현하지 마라" 시스템 메시지는 사라졌다 — #770 에서 런의 지시는 '만들어라',
주입된 메시지는 '만들지 마라' 였고 결과는 명세를 받고 완료를 듣는 것이었다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from tests._support import memory_session

_STEPS = [
    {"stage": "design", "intent": "결제 흐름과 실패 처리를 설계한다"},
    {"stage": "impl", "intent": "설계대로 결제 엔드포인트를 구현한다"},
]


async def _seed_run(
    session: Any,
    *,
    steps: list[dict[str, str]] | None = None,
    step_index: int = 0,
    refs: list[str] | None = None,
) -> ExecutionRun:
    frame: dict[str, Any] = {"artifact_type_hint": "code", "path_classification": "agent_loop"}
    if steps is not None:
        frame["steps"] = steps
    payload: dict[str, Any] = {
        "intent_text": "결제 시스템을 만들어줘. 실패 처리와 재시도까지 포함해서.",
        "frame": frame,
    }
    if steps:
        payload["stage"] = steps[step_index]["stage"]
        payload["step_index"] = step_index
        payload["step_intent"] = steps[step_index]["intent"]
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,  # non-product → transition skips auto-ship, still chains
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload=payload,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    if refs:
        session.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"artifact_refs": refs},
                created_at=datetime.now(tz=UTC),
            )
        )
        await session.flush()
    return run


async def _spawned(session: Any, *, exclude: uuid.UUID) -> list[ExecutionRun]:
    rows = (await session.execute(select(ExecutionRun).where(ExecutionRun.id != exclude))).scalars()
    return list(rows)


async def test_a_finished_step_spawns_the_next_one() -> None:
    async with memory_session() as s:
        first = await _seed_run(s, steps=_STEPS, refs=["docs/spec.md", "docs/api.md"])

        await AgentRunner(s).transition(run_id=first.id, to_status=RunStatus.REVIEW_READY)

        spawned = await _spawned(s, exclude=first.id)
        assert len(spawned) == 1
        nxt = spawned[0]
        assert nxt.status is RunStatus.OPEN
        assert nxt.workspace_id == first.workspace_id
        assert nxt.request_id == first.request_id
        assert nxt.payload["stage"] == "impl"
        assert nxt.payload["step_index"] == 1
        assert nxt.payload["prior_run_id"] == str(first.id)
        assert nxt.payload["prior_artifact_refs"] == ["docs/spec.md", "docs/api.md"]


async def test_the_next_run_works_under_its_own_step_intent() -> None:
    """스텝의 지시문이 곧 그 런의 지시문이다 — 주입되는 시스템 메시지가 아니라."""
    async with memory_session() as s:
        first = await _seed_run(s, steps=_STEPS, refs=["spec.md"])

        await AgentRunner(s).transition(run_id=first.id, to_status=RunStatus.REVIEW_READY)

        nxt = (await _spawned(s, exclude=first.id))[0]
        assert nxt.payload["step_intent"] == "설계대로 결제 엔드포인트를 구현한다"


async def test_the_plan_travels_so_a_third_step_can_follow() -> None:
    """체인 길이는 2로 고정돼 있지 않다 — 형님 룰이 세 단계를 구분하면 셋이다."""
    steps = [
        {"stage": "design", "intent": "설계"},
        {"stage": "impl", "intent": "구현"},
        {"stage": "review", "intent": "검토"},
    ]
    async with memory_session() as s:
        second = await _seed_run(s, steps=steps, step_index=1, refs=["a.py"])

        await AgentRunner(s).transition(run_id=second.id, to_status=RunStatus.REVIEW_READY)

        nxt = (await _spawned(s, exclude=second.id))[0]
        assert nxt.payload["stage"] == "review"
        assert nxt.payload["step_index"] == 2
        assert nxt.payload["frame"]["steps"] == steps


async def test_the_last_step_spawns_nothing() -> None:
    """음성 대조군 — 목록 끝에서 멈춘다. 무한 체인이 아니다."""
    async with memory_session() as s:
        last = await _seed_run(s, steps=_STEPS, step_index=1, refs=["x.py"])

        await AgentRunner(s).transition(run_id=last.id, to_status=RunStatus.REVIEW_READY)

        assert await _spawned(s, exclude=last.id) == []


async def test_an_unsplit_run_spawns_nothing() -> None:
    """형님 워크스페이스의 현재 상태 — 룰이 없어 쪼개지 않은 런."""
    async with memory_session() as s:
        run = await _seed_run(s, steps=None, refs=["x.py"])

        await AgentRunner(s).transition(run_id=run.id, to_status=RunStatus.REVIEW_READY)

        assert await _spawned(s, exclude=run.id) == []


async def test_a_single_step_plan_spawns_nothing() -> None:
    """한 스텝은 쪼갠 게 아니다."""
    async with memory_session() as s:
        run = await _seed_run(s, steps=[{"stage": "impl", "intent": "구현"}], refs=["x.py"])

        await AgentRunner(s).transition(run_id=run.id, to_status=RunStatus.REVIEW_READY)

        assert await _spawned(s, exclude=run.id) == []


async def test_spawn_with_no_deliverable_yields_empty_refs() -> None:
    async with memory_session() as s:
        first = await _seed_run(s, steps=_STEPS, refs=None)

        await AgentRunner(s).transition(run_id=first.id, to_status=RunStatus.REVIEW_READY)

        nxt = (await _spawned(s, exclude=first.id))[0]
        assert nxt.payload["prior_artifact_refs"] == []


async def test_spawn_inlines_the_prior_steps_output(tmp_path: Any) -> None:
    """D-2 그대로 — 산출물 TEXT 를 spawn 시점에 인라인한다. 나중에 읽으면
    워크트리 정리와 레이스하고, 보류된 런의 산출물은 main 에 도달하지 않는다."""
    from pathlib import Path
    from types import SimpleNamespace

    from backend.storage.artifact_store import LocalFilesystemArtifactStore

    settings = SimpleNamespace(
        product_workspace_root=str(tmp_path / "products"),
        run_workspace_root=str(tmp_path / "runs"),
    )
    async with memory_session() as s:
        first = await _seed_run(s, steps=_STEPS, refs=["docs/spec.md"])
        LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
            first.id, "docs/spec.md", "# Spec\n결제 흐름은 이렇게 간다.\n".encode()
        )

        await AgentRunner(s, settings=settings).transition(  # type: ignore[arg-type]
            run_id=first.id, to_status=RunStatus.REVIEW_READY
        )

        nxt = (await _spawned(s, exclude=first.id))[0]
        assert "결제 흐름은 이렇게 간다." in nxt.payload["prior_output_text"]


async def test_spawn_inlines_none_when_the_output_is_unreadable(tmp_path: Any) -> None:
    """refs 는 있는데 읽을 파일이 없으면 정직하게 None — 다음 스텝은 그래도 간다."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        product_workspace_root=str(tmp_path / "products"),
        run_workspace_root=str(tmp_path / "runs"),
    )
    async with memory_session() as s:
        first = await _seed_run(s, steps=_STEPS, refs=["docs/spec.md"])  # 디스크에 없다

        await AgentRunner(s, settings=settings).transition(  # type: ignore[arg-type]
            run_id=first.id, to_status=RunStatus.REVIEW_READY
        )

        nxt = (await _spawned(s, exclude=first.id))[0]
        assert nxt.payload["prior_output_text"] is None
        assert nxt.payload["prior_artifact_refs"] == ["docs/spec.md"]


async def test_the_founders_full_request_survives_every_step() -> None:
    """#690 — 형님의 지시는 **절대** 잘리거나 대체되지 않는다.

    스텝의 brief 로 ``intent_text`` 를 덮어쓰면 그 brief 는 프레이머가 쓴
    요약이고, 형님이 쓴 요구사항 중 그 요약에 안 들어간 것은 그 지점에서
    영원히 사라진다. #690 이 512자 truncation 으로 같은 손실을 이미 쟀다 —
    에이전트는 받은 절반만 만들었고 그 절반 위에서 lint/test 는 통과했다.

    그래서 ``intent_text`` 는 체인 내내 형님 원문이고, 스텝의 brief 는
    ``step_intent`` 로 따로 간다.
    """
    async with memory_session() as s:
        first = await _seed_run(s, steps=_STEPS, refs=["spec.md"])
        original = first.payload["intent_text"]

        await AgentRunner(s).transition(run_id=first.id, to_status=RunStatus.REVIEW_READY)

        nxt = (await _spawned(s, exclude=first.id))[0]
        assert nxt.payload["intent_text"] == original
        assert nxt.payload["step_intent"] == "설계대로 결제 엔드포인트를 구현한다"
