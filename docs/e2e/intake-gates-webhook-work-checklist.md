# E2E — 웹훅 일은 인테이크에서 상한·예산을 만난다 (#1113)

GitHub App 이후 바인딩된 레포의 이슈·PR·댓글은 전부 일이 된다 — **의도다** (형님 2026-10-06: "일단 받고, 단순 질의거나
의미 없으면 종료될 테니"). 그런데 동시 런 상한과 월 토큰 예산은 Direct 두 입구에서만 읽혀서, 웹훅은 둘 다 밖에서 돌았다.

형님 결정: **상한은 대기, 예산은 거절.**

밑에 있던 결함 하나: 인테이크 claim(`list_undrained`)이 Request 없는 트리거를 **오래된 순 50개** 가져온 뒤에야 필터 거절분을
걸러냈다. 거절된 트리거는 Request 가 영영 없으니 그 창에 계속 남는다 — 50개가 쌓이면(일시정지 PR 마다 하나씩 남는다)
**인테이크가 새 트리거를 다시는 보지 못한다**. 대기 트리거도 같은 식으로 쌓이므로 둘 다 SQL 에서 뺀다.

## 바뀐 것

- `list_undrained` — `_received_filtered` · `_intake_held` 표시가 있는 트리거를 SQL(limit 전)에서 제외
- `application/intake_gate.py` — 웹훅 트리거만:
  - 월 토큰 예산 소진 → **거절**: `_received_filtered{reason: token_budget_reached}` + `needs_you` 알림("이번 달 토큰 예산을 다 써서 … 시작하지 않았어요")
  - 동시 런 상한 꽉 참 → **대기**: `_intake_held{reason: run_cap_reached}`. 상한은 보유 런 + 아직 런이 안 된 `open` Request 로 센다
- `IntakeWorker._release_held` — 매 틱, 대기 트리거를 오래된 순으로 빈 슬롯 수만큼 풀어 같은 틱에 Request 로
- Direct(형님 직접)·스케줄·다음 스텝은 그대로 (Direct 는 입구에서 429)

## 검증 (로컬)

- [x] RED → GREEN — `tests/glue/test_intake_gates_webhook_work.py` (10, RED 6 + 대조군)
  - 거절 트리거 3개 + 새 트리거, batch 3 → 새 트리거 처리 (main 에서 RED — 실제 결함)
- [x] 전선 절단 7곳(거절 제외 · 대기 제외 · 웹훅 게이트 · 거절 분기 · 슬롯 수만큼 · 해제 호출 · open Request 계산), 각각 빨강
- [x] 일회용 Postgres 컨테이너에서 같은 테스트 + receive stage + #1112 테스트 29 passed (JSON `->>` 경로 확인)
- [x] ruff · mypy · lint-imports

## 배포 후 (prod)

- [ ] 인테이크 로그에 새 트리거가 계속 처리된다 (`intake_worker_request_created` / `intake_worker_trigger_filtered`)
- [ ] 상한이 꽉 찬 상태에서 이슈를 열면 `intake_worker_trigger_held`, 런 하나가 끝나면 `intake_worker_trigger_released` → 런
- [ ] (예산 소진 시) `intake_worker_trigger_refused` + 폰에 needs_you 알림

## 남은 틈

- 대기 중인 트리거는 PWA 에 보이지 않는다 (로그만). 풀리면 기존 `triggered` 알림이 간다
- 스케줄 틱·다음 스텝은 여전히 상한 밖 (`run_caps` docstring 의 의도)
