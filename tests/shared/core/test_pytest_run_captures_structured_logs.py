"""테스트 런의 구조화 로그가 **파일로** 남는가 — #950/#967 의 공통 선행작업.

#950 은 다섯 달 된 flake 의 두 원인(*"워커가 보고를 안 했다"* vs *"보고가 안 닿았다"*)을
가르는 신호로 워커 쪽 `executor_result_post_rejected` 를 지목했다. 그런데 CI 로그에서
그걸 셌더니 0건이었고, **음성 대조군 `event=` 도 0건**이었다 — 애플리케이션 구조화
로그가 애초에 캡처되지 않는다. 그러니 그 0 은 *안 일어났다*가 아니라 **잴 수 없다**다.

원인은 두 겹이다.

1. **아무도 `configure_logging` 을 안 부른다.** 테스트 런의 structlog 은 설정되지 않은
   채 **라이브러리 기본값**(개발자 터미널용 ConsoleRenderer)으로 돈다 — 워커 prod 로그가
   776MB 로 불었던 것과 같은 축이다.
2. **pytest 가 stdout 을 삼킨다.** 실패한 테스트의 캡처 섹션에만 일부가 보이고,
   통과한 테스트의 로그는 어디에도 남지 않는다. ⇒ stdout 으로는 CI 아티팩트를 못 만든다.

그래서 파일로 쓴다. 이 모듈은 **그 파일이 실제로 채워지는가**를 단언한다 —
설정만 해 두고 아무것도 안 닿으면, 다음 사람이 또 0 을 세고 또 오진한다.
"""

from __future__ import annotations

import json

import structlog

# ⭐ 모듈 임포트 시점에 잡은 로거. 프로덕션 모듈이 하는 것과 같다
# (`logger = structlog.get_logger(__name__)` 가 import 시 실행된다).
# conftest 의 설정은 **이보다 나중에** 돈다 — 늦은 설정이 이미 만들어진 로거에도
# 닿는지가 이 파일의 진짜 명제고, `cache_logger_on_first_use=True` 로 바뀌는 순간
# 조용히 깨지는 자리다.
_module_logger = structlog.get_logger(__name__)


def _lines(path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            # JSON 이 아닌 줄이 섞이면 그것 자체가 결함이다 — 아래에서 센다
            out.append({"_unparsed": raw})
    return out


def test_the_log_file_exists_and_is_being_written(structlog_capture_path) -> None:
    """양성 대조군 — 파일이 실재하고 이미 뭔가 들어 있다.

    이게 없으면 아래 단언들이 *"내가 방금 쓴 줄"* 하나만 보고 통과할 수 있다.
    """
    assert structlog_capture_path.exists(), (
        f"capture file was never created: {structlog_capture_path}"
    )


def test_an_event_from_an_import_time_logger_lands_in_the_file(
    structlog_capture_path,
) -> None:
    """늦은 설정이 **이미 만들어진** 로거에도 닿는다."""
    marker = "test_capture_marker_import_time"
    _module_logger.info(marker, probe=1)

    events = [e for e in _lines(structlog_capture_path) if e.get("event") == marker]
    assert events, f"{marker} never reached {structlog_capture_path}"
    assert events[-1]["probe"] == 1
    # JSON 한 줄 = 한 이벤트. 개발용 렌더러로 돌면 이 키들이 없다
    assert events[-1]["level"] == "info"
    assert "timestamp" in events[-1]


def test_a_freshly_bound_logger_also_lands(structlog_capture_path) -> None:
    """대조군의 반대쪽 — 런타임에 만든 로거도 같은 파일로 간다."""
    marker = "test_capture_marker_runtime"
    structlog.get_logger("runtime.probe").warning(marker, probe=2)

    events = [e for e in _lines(structlog_capture_path) if e.get("event") == marker]
    assert events, f"{marker} never reached {structlog_capture_path}"
    assert events[-1]["level"] == "warning"


def test_every_captured_line_is_json(structlog_capture_path) -> None:
    """아티팩트는 **기계가 읽을 수 있어야** 한다.

    #950 이 하려던 일은 `grep`/`jq` 로 한 이벤트를 세는 것이다. 개발용 렌더러가
    끼어들어 한 예외가 14줄로 퍼지면 그 집계가 불가능해진다.
    """
    unparsed = [e["_unparsed"] for e in _lines(structlog_capture_path) if "_unparsed" in e]
    assert not unparsed, f"non-JSON lines in the capture file: {unparsed[:3]}"
