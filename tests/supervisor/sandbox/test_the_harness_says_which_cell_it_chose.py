"""하네스가 **어느 칸을 골랐는지** 아티팩트에 남기는가 — #950 의 다음 관문.

#950 은 다섯 달 된 flake 를 판정표로 좁혀 왔다. 2026-09-23 에 #1040(구조화 로그
아티팩트)이 붙은 뒤 첫 빨강에서 두 칸이 배제됐고, 남은 질문은 *"왜 워커가 클레임을
안 했나"* 다. 그런데 그 빨강을 다시 읽으니 **두 증거가 서로 모순이었다**:

* 실패 문구는 **제품 쪽 단언**(``assert result.timed_out is False``)이었다. 즉
  ``_worker_then`` 의 ``await worker`` 도, ``_attribute_a_late_worker`` 도 **안 던졌다**
  ⇒ 판별자는 *"워커가 제때 기록했다"* 로 판정했다는 뜻이다.
* 같은 런의 아티팩트에는 그 태스크의 ``executor_task_result_recorded`` 가 **없고**
  ``last_status`` 가 70초 내내 ``dispatched`` 였다 ⇒ 기록은 없었다는 뜻이다.

둘 중 하나는 틀렸는데 **가를 수가 없다.** 이유는 단순하다: **판별자는 실패할 때만
말한다.** 통과로 판정하면 조용히 ``return`` 하고 아무 행도 안 남긴다 — 그래서
아티팩트에는 "하네스가 무엇을 봤는가"가 통째로 빠져 있다. 통과만 세는 요약이
안 돌아간 검사를 숨기는 것과 같은 모양이다.

이 모듈이 고정하는 명제는 둘이다.

1. **판별자는 모든 판정에서 행을 남긴다** — 통과 판정도 포함해서, 그리고 세 판정이
   서로 **다른 값**으로 구분된다.
2. **가짜 워커의 클레임 마일스톤은 ``task_id`` 를 들고 있다** — 그래야 아티팩트에서
   제품 쪽 ``executor_task_*`` 와 **조인**되고, "누가 먼저였나"를 시각이 아니라
   같은 키로 물을 수 있다.

⚠️ 이름은 전부 ``harness_`` 로 시작한다. 하네스의 실패가 제품의 얼굴을 쓰면 안 되듯,
하네스의 **계측**도 제품 이벤트 이름 공간을 침범하면 안 된다 — 아티팩트를 세는 쪽이
그 둘을 합산하는 순간 이 이슈가 네 번째로 오진한다.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from ..._support import shared_file_sessionmaker
from .test_client_worker_manager import (
    _attribute_a_late_worker,
    _make_redis,
    _make_session,
    _run_one_exec_task,
    _seed_worker,
    _worker_then,
)

#: 판별자가 남기는 이벤트 이름. 테스트가 이 상수를 쓰는 이유는 오타로 초록이 되는
#: 것을 막기 위해서가 아니라, **이름이 바뀌면 아티팩트를 세는 쿼리도 같이 바뀌어야
#: 한다는 것**을 한 곳에 묶어 두기 위해서다.
_VERDICT_EVENT = "harness_worker_attribution"


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [line for line in logs if line.get("event") == name]


# ---------------------------------------------------------------------------
# ① 판별자는 모든 판정에서 말한다
# ---------------------------------------------------------------------------


def test_the_passing_verdict_leaves_a_row() -> None:
    """워커가 제때 기록했다는 판정도 **행으로 남아야 한다.**

    이게 없어서 2026-09-23 빨강을 다시 읽을 수 없었다: 판별자가 조용히 통과시켰다는
    사실 자체가 아티팩트 어디에도 없어, 제품 쪽 증거와 모순을 가릴 수 없었다.
    """
    with capture_logs() as logs:
        _attribute_a_late_worker({"seen_at": 1.0, "recorded_at": 2.0}, started=0.0, gave_up_at=70.0)

    rows = _events(logs, _VERDICT_EVENT)
    assert len(rows) == 1, (
        f"통과 판정이 행을 안 남겼다 (남은 이벤트: {[x.get('event') for x in logs]})"
    )
    assert rows[0]["verdict"] == "worker_kept_up"


def test_the_blaming_verdict_leaves_the_same_row_before_it_raises() -> None:
    """하네스를 탓하는 판정도 **같은 이벤트**로 남는다 — 예외 문구는 pytest 출력에만
    남고 아티팩트에는 안 들어간다. 두 판정이 같은 이름/다른 값이어야 한 번의 집계로
    센다."""
    with capture_logs() as logs, pytest.raises(AssertionError, match="AFTER the product gave up"):
        _attribute_a_late_worker(
            {"seen_at": 1.0, "recorded_at": 75.0}, started=0.0, gave_up_at=70.0
        )

    rows = _events(logs, _VERDICT_EVENT)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "harness_recorded_late"


def test_a_never_recorded_verdict_is_told_apart_from_a_late_one() -> None:
    """*"아예 기록 안 했다"* 와 *"늦게 기록했다"* 는 **다른 칸**이다 — 전자는 워커가
    태스크를 못 봤다는 쪽이고 후자는 러너가 느렸다는 쪽이다. 한 값으로 뭉치면 다음
    사람이 또 두 칸을 못 가른다."""
    with capture_logs() as logs, pytest.raises(AssertionError, match="recorded: never"):
        _attribute_a_late_worker({"seen_at": 1.0}, started=0.0, gave_up_at=70.0)

    rows = _events(logs, _VERDICT_EVENT)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "harness_never_recorded"


def test_the_verdict_row_carries_the_timings_it_judged_on() -> None:
    """판정만 있고 숫자가 없으면 다음 사람이 그 판정을 **검산할 수 없다.**

    ``started`` 기준 상대 초로 적는다 — 절대 시각은 런마다 달라 비교가 안 된다.
    """
    with capture_logs() as logs:
        _attribute_a_late_worker(
            {"seen_at": 1.5, "recorded_at": 2.25}, started=1.0, gave_up_at=70.0
        )

    row = _events(logs, _VERDICT_EVENT)[0]
    assert row["seen_at_s"] == pytest.approx(0.5)
    assert row["recorded_at_s"] == pytest.approx(1.25)
    assert row["gave_up_at_s"] == pytest.approx(69.0)


def test_a_worker_that_never_saw_the_task_says_so_in_the_row() -> None:
    """``seen_at`` 부재는 ``None`` 으로 남는다 — 키가 통째로 빠지면 집계하는 쪽이
    *"안 봤다"* 와 *"이 버전은 그 필드를 안 찍는다"* 를 구분 못 한다."""
    with capture_logs() as logs, pytest.raises(AssertionError):
        _attribute_a_late_worker({}, started=0.0, gave_up_at=70.0)

    row = _events(logs, _VERDICT_EVENT)[0]
    assert row["seen_at_s"] is None
    assert row["recorded_at_s"] is None


# ---------------------------------------------------------------------------
# ② 가짜 워커의 클레임 경로가 task_id 를 들고 말한다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fake_worker_names_the_task_it_claimed_and_recorded(tmp_path: Path) -> None:
    """수신·기록 두 마일스톤이 **같은 ``task_id``** 를 들고 남는다.

    이게 조인 키다. 제품 쪽은 ``executor_task_created`` / ``..._dispatched`` /
    ``..._result_recorded`` 를 ``task_id`` 로 찍는다 — 하네스가 같은 키를 찍어야
    *"디스패치된 그 태스크를 하네스가 언제 집었나"* 를 아티팩트만으로 물을 수 있다.
    2026-09-23 빨강에서 그 질문이 막힌 자리가 정확히 여기다.
    """
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        with capture_logs() as logs:
            result = await _worker_then(
                redis, factory, wid, box.exec("echo ok", timeout_s=10.0, shell=True)
            )
    await redis.aclose()

    assert result.exit_code == 0, "이 테스트의 전제(정상 경로)가 깨졌다"

    saw = _events(logs, "harness_worker_saw_task")
    recorded = _events(logs, "harness_worker_recorded")
    assert len(saw) == 1, "수신 마일스톤이 없다"
    assert len(recorded) == 1, "기록 마일스톤이 없다"
    assert saw[0]["task_id"] == recorded[0]["task_id"], "두 마일스톤이 같은 태스크를 안 가리킨다"
    # 제품 이벤트와 같은 키 공간(문자열 uuid)이어야 조인된다.
    uuid.UUID(saw[0]["task_id"])


@pytest.mark.asyncio
async def test_the_fake_worker_that_saw_nothing_leaves_a_row_before_it_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """예산을 태우고 죽는 경로도 **행을 남긴다.**

    지금은 AssertionError 문구에만 숫자(xread 횟수·본 action)가 있다. 그 문구는
    pytest 출력에만 남고 **아티팩트에는 안 들어간다** — 그래서 '하네스가 굶었나
    엉뚱한 스트림을 봤나'를 아티팩트만으로는 못 가른다.
    """
    import tests.supervisor.sandbox.test_client_worker_manager as mod

    monkeypatch.setattr(mod, "_FAKE_WORKER_BUDGET_S", 0.3)
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        with capture_logs() as logs, pytest.raises(AssertionError):
            await _run_one_exec_task(redis, factory, uuid.uuid4())
    await redis.aclose()

    rows = _events(logs, "harness_worker_gave_up")
    assert len(rows) == 1, "포기 판정이 행을 안 남겼다"
    assert rows[0]["reads"] >= 1, "xread 를 몇 번 돌았는지가 굶주림의 유일한 센서다"
    assert rows[0]["actions_seen"] == []
    assert "stream" in rows[0], "엉뚱한 스트림을 봤는지 가르려면 스트림 이름이 있어야 한다"


# ---------------------------------------------------------------------------
# ③ 살아 있는 판정과 합성 예시를 **구조로** 가른다
# ---------------------------------------------------------------------------
#
# 판별자를 핀으로 박는 유닛테스트(``test_a_worker_that_recorded_too_late_is_named``
# 등)는 합성 값으로 판별자를 직접 부른다 — 그래서 **완전히 초록인 런에서도**
# ``harness_recorded_late`` 행이 아티팩트에 남는다. 다음 사람이 그걸 세면 있지도
# 않은 하네스 지연을 발견한다.
#
# 다음 사람이 ``capture_logs`` 를 잊지 않기를 바라는 것은 규율이지 구조가 아니다.
# 대신 **조인 키를 liveness 마커로 겸용**한다: 실제 워커를 거친 판정만 ``task_id``
# 를 들 수 있다. 집계하는 쪽의 규칙은 한 줄이 된다 — *task_id 없는 판정 행은
# 살아 있는 판정이 아니다.*


def test_a_synthetic_verdict_carries_no_task_id() -> None:
    with capture_logs() as logs:
        _attribute_a_late_worker({"seen_at": 1.0, "recorded_at": 2.0}, started=0.0, gave_up_at=70.0)

    assert _events(logs, _VERDICT_EVENT)[0]["task_id"] is None


@pytest.mark.asyncio
async def test_a_live_verdict_carries_the_task_it_judged(tmp_path: Path) -> None:
    """실제 디스패치를 거친 판정은 **그 태스크 id** 를 든다.

    이게 있으면 아티팩트에서 판정 행이 제품 쪽 ``executor_task_dispatched`` 와
    직접 조인된다 — #950 이 2026-09-23 에 물어야 했으나 물을 수 없었던 질문
    (*"그 태스크에 대해 하네스는 무엇이라 판정했나"*)이 한 번의 조인이 된다.
    """
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        with capture_logs() as logs:
            await _worker_then(redis, factory, wid, box.exec("echo ok", timeout_s=10.0, shell=True))
    await redis.aclose()

    verdict = _events(logs, _VERDICT_EVENT)[0]
    saw = _events(logs, "harness_worker_saw_task")[0]
    assert verdict["task_id"] == saw["task_id"], "판정 행이 어느 태스크에 대한 것인지 말하지 않는다"
