# E2E — 런의 상태는 한 함수에서만 바뀐다 (#1110)

#1102 가 `AgentRunner.transition()` 을 compare-and-set 으로 만들었지만, 여섯 곳이 `run.status` 를 직접 쓰고
자기 이력 행을 따로 남겼다. 이 경로들에는 취소 가드도 CAS 도 없었다 — 다른 세션이 커밋한 취소를 여전히
덮어쓸 수 있었다.

| 자리 | 전이 |
|---|---|
| `agent_runner._auto_ship_product_run` | review_ready → shipped |
| `run_delivery_resolution` (두 경로) | → review_ready → shipped |
| `run_cleanup._cancel` / `_reopen` | → cancelled / → open |
| `safe_mode_queue` 거절 후 재개 | → open |
| `agent_worker` 드라이브 실패 에스컬레이션 | open → running |

## 바뀐 것

- `backend/workflow/application/run_status.move_run_status` — 상태 변경의 유일한 자리
  - `UPDATE … WHERE id = :id AND status = :from`(붙잡힌 객체의 상태) — 0행이면 거절, 객체를 새로고침
  - 이력 행은 이동이 성공했을 때만
  - 성공 시 `set_committed_value` 로 객체 반영(dirty 로 두면 조건 없는 UPDATE 가 한 번 더 나간다)
  - leaf 모듈(sqlalchemy + ORM 행만) — 인바운드 웹훅·MCP 쪽 호출자도 쓸 수 있다
- `transition()` 과 여섯 자리가 모두 이 함수를 쓴다. 이동이 거절되면 각 자리는 그 사실을 반환한다
  (취소 `False` · 배송 자동 해소 `False` · 에스컬레이션은 로그 후 중단)
- **가드** — `tests/glue/test_one_run_state_machine.py::test_no_run_status_is_written_outside_the_one_function`
  - `run.status = …` · `<x>.status = RunStatus.X` · `from_status` 가 있는 `ExecutionRunHistory(` (생성 행은 `from_status=None` 이라 허용)
    · `.values(status=RunStatus.X)` 를 `run_status.py` 밖에서 찾는다
  - 예외 둘: `agent_worker` 의 대량 claim(OPEN→RUNNING)·stale reaper(RUNNING→OPEN) — 같은 트랜잭션의
    `SELECT … FOR UPDATE SKIP LOCKED` 로 행이 잠겨 이미 CAS 다. (파일, 설정 상태) 쌍으로 적어 새 대량 쓰기는 잡힌다

## 검증 (로컬)

- [x] RED → GREEN
  - `move_run_status` 단위: 이동 · 이력 · 객체 반영 / 다른 세션이 먼저 옮기면 거절 + 객체가 진실을 본다 / 같은 상태 no-op
  - 가드는 구현 전에 정확히 여섯 자리 + `transition()` 의 이력 행(12개 위반)을 찾았다
  - 가드 대조군: 가짜 위반 파일에서 빨개진다
- [x] 전선 절단 2곳 — CAS 조건 제거(4 failed) · 직접 쓰기 재도입(가드 1 failed), 9 수집
- [x] ruff · mypy · import-linter 6/6

## 배포 후 (prod)

워커 변경 없음 — 바뀐 자리는 모두 백엔드·워커 컨테이너(autodeploy).

- [ ] 로그에 `run_status_move_lost_race` 가 찍히면 그 런의 최종 상태가 `actual_status` 와 같고, 이력에 거절된
  전이가 없다 — 자연 발생을 기다린다
- [ ] 정상 경로 회귀 없음: 다음 자동 배송(로컬 auto-ship · github 배송 자동 해소)이 `shipped` 로 끝난다

## 남은 틈

- **#1109:** 어떤 전이가 허용되는지(`shipped` → `open` 금지)는 아직 없다. `move_run_status` 가 그 표가 들어갈 자리다
- 생성 이력 행(`from_status=None`)은 가드 밖이다 — 새 런을 만드는 것은 상태를 옮기는 것이 아니다
