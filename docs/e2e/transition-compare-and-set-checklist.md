# E2E — 다른 세션이 커밋한 취소를 루프가 덮어쓰지 않는다 (#1102)

2026-09-30 실측, 런 `5044c88a`: 08:36 취소 → 08:37:51 이력에 `running → review_ready`.
세 번째 행의 `from_status` 가 `cancelled` 가 아니라 `running` 이다. 전이를 쓴 쪽이 취소를 보지 못했다.

## 원인

`AgentRunner.transition()` 은 `session.get` 이 돌려준 **identity map 의 객체**로 가드를 판정했다.
드라이브 루프의 세션은 런을 붙잡고 턴 경계마다 `expire_on_commit=False` 로 커밋하므로, 그 객체는
다른 세션(형님의 취소 요청)이 `cancelled` 를 커밋한 뒤에도 `running` 이었다. 가드는 통과했고,
ORM flush 는 조건 없는 `UPDATE … SET status` 라 취소를 덮어썼다.

## 바뀐 것

- `transition()` 의 상태 쓰기를 **compare-and-set** 으로: `UPDATE … WHERE id = :id AND status = :from`
  - 영향 행 0 → 다른 쓰기가 먼저 옮겼다. no-op, `False`, `agent_runner_transition_lost_race` 로그
  - 그 경우 붙잡힌 객체의 `status` 를 새로고침해 호출자가 진실을 본다
  - 성공 시 객체에 `set_committed_value` 로 반영한다(dirty 로 두면 조건 없는 UPDATE 가 한 번 더 나간다)
- 이력 행은 CAS 가 성공한 뒤에만 쓴다 — 일어나지 않은 전이를 기록하지 않는다
- `review_ready` 와 `failed` 둘 다 같은 경로를 탄다(이슈의 3번 항목)

## 검증 (로컬)

- [x] RED → GREEN — `tests/glue/test_transition_is_compare_and_set.py`
  - 다른 세션의 취소 뒤 루프의 `review_ready` / `failed` 가 no-op, DB 는 `cancelled`
  - 거절된 전이의 이력 행이 없다
  - 대조군: 경쟁이 없으면 전이가 된다
- [x] ⚠️ 테스트가 처음엔 경합을 재현하지 못했다. `await loop.get(...)` 의 결과를 버리면 identity map 이
  **약한 참조**라 객체가 수거되고, 다음 `get` 이 DB 에서 새로 읽어 `cancelled` 를 본다. 루프처럼 참조를
  붙잡아야 경합이 재현된다
- [x] 전선 절단 2곳(조건의 `status ==` 제거 · ORM 쓰기로 되돌림), 각각 3 failed / 4 수집
- [x] 기존 `test_run_cancel.py` · `test_handoff_chain.py` 통과
- [ ] **PostgreSQL:** 로컬에서는 SQLite 로 돌았다. CI 의 `lint-and-test` 가 pgvector:pg16 서비스로 같은
  테스트를 돌린다 — PR 의 CI 결과로 확인

## 배포 후 (prod)

워커 변경 없음(전이는 워커 컨테이너의 드라이브 루프와 백엔드에서 돈다 — 둘 다 autodeploy).

- [ ] 에이전트 루프가 끝나기 직전에 취소한 런이 `cancelled` 로 남고, 이력에 `review_ready` 행이 없다
  - #1106 이후로는 실행기 턴 도중 취소가 세션을 먼저 죽이므로, 이 경합이 남는 곳은 **검증 단계**
    (턴이 끝나고 검증이 도는 사이의 취소)다
  - ⚠️ prod 런을 일부러 여는 행동이므로 형님 OK 를 받은 뒤
- [ ] 로그에 `agent_runner_transition_lost_race` 가 찍히면 그 런의 최종 상태가 `actual_status` 와 같다

## 남은 틈

- **#1110:** 상태 전이가 `transition()` 을 우회해 이력을 직접 쓰는 곳이 여섯 곳이다. 그 경로들은 이
  CAS 를 받지 않는다
- **#1109:** `shipped` 런이 merge-watch 재배송으로 `open` 으로 되돌아간다 — CAS 는 "누가 먼저 옮겼나"만
  막고, 어떤 전이가 허용되는지(전이 표)는 다루지 않는다
