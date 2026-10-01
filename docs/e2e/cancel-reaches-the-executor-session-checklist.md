# E2E — 런 취소가 실행 중인 실행기 세션에 닿는다 (#1106)

2026-09-30 실측: 런 `58a7426d` 를 10:41:01 에 취소했지만 그 Claude Code 세션은 10:52:22 까지
돌았다(입력 토큰 6,403,534). 런 스코프 MCP 토큰으로 `file_edit` / `shell_exec` / `file_write`
도 계속 받아들여졌다. 취소는 DB 행만 바꿨다.

두 갈래로 막는다.
- **세션 종료:** `ExecutorAdapter` 가 런에 묶인 턴을 기다리는 동안, 폴링 틱마다(2초) 런 상태를
  확인한다. CANCELLED 이면 태스크 행을 닫고 워커에 `cancel_task` 를 보낸다(타임아웃 경로가
  쓰던 같은 kill 신호). 그다음 `RunCancelledDuringTurn` 을 던진다. 이 예외는 재시도하지 않고,
  드라이브 루프는 이것을 "취소됨" 으로 끝낸다.
- **토큰 거절:** 모든 work 도구가 거치는 `load_run` 이 종결 상태(cancelled / failed / shipped)
  런을 거절한다.

## 검증 (로컬)

- [x] RED → GREEN:
  - `tests/mcp/test_work_tools_refuse_a_finished_run.py` (종결 3종 거절, 진행 중 2종 통과 대조군)
  - `tests/executors/test_awaiter_abandons_when_asked.py`
    - 참이면 포기하고 행을 `failed` 로 닫는다
    - 거짓이면 기존처럼 타임아웃
    - 프로브가 터지면 계속 기다린다
    - 이미 끝난 결과는 프로브보다 우선
  - `tests/dispatch/test_adapter_stops_a_cancelled_runs_session.py` (kill 1회 · 재시도 없음 · run 없는 어댑터는 프로브 없음)
  - `tests/execution/test_run_orchestrator.py::test_cancel_during_an_executor_turn_ends_the_loop_as_cancelled`
- [x] 전선 절단: 각 배선을 끊으면 해당 테스트가 빨개진다. 수집은 정상이고 해당 테스트만 실패한다.
  - 어댑터의 `abandon_if=` 인자 제거
  - 프로브가 항상 False 를 반환
  - 루프의 `except RunCancelledDuringTurn` 제거
- [x] 재전송(#965) 동작 보존: `tests/executors` 553 passed
- [x] import-linter 6/6 kept · ruff · mypy

## 배포 후 (prod)

워커 쪽 변경은 없다. 워커의 cancel 처리(Lift E14)는 이미 있다. 그래서 백엔드 autodeploy 만으로 반영된다.

- [ ] 런 하나를 시작하고 실행기 태스크가 `dispatched` 된 뒤 취소한다. 확인할 것:
  - 수 초 안에 로그에 `executor_adapter_run_cancelled_mid_turn` → `dispatch_cancel_xadd_succeeded` → (워커) `worker_task_cancel_started` 가 찍힌다
  - `executor_tasks` 행이 `failed` 가 되고 `error_message` 에 `abandoned:` 가 들어간다
  - 취소 이후 그 런의 `mcp_work_tool` 호출이 없다. 들어온다면 `mcp_work_run_finished` 로 거절돼야 한다
  - ⚠️ prod 런을 일부러 여는 행동이므로 형님 OK 를 받은 뒤 진행한다
- [ ] 회귀 관측: 취소하지 않은 런은 계속 완료된다(`abandoned:` 행이 취소 런에만 생긴다)

## 남은 틈

- pub/sub 가 실패해서 순수 DB 폴링(`_poll_until_terminal`)으로 강등된 대기에는 프로브가 없다.
  드문 경로라 이번 범위에서 뺐다.
- 백엔드 프로세스가 대기 중에 재시작되면 기다리던 쪽이 사라진다. 이 경우 세션 kill 은 일어나지
  않는다. 그래도 토큰 거절은 여전히 적용되므로 세션이 런 워크트리를 수정하지는 못한다.
