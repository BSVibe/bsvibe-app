# E2E — 배송될 것이 없는 런은 런 슬롯을 놓는다 (#1115)

prod: 누적 취소 226건 중 73건(1/3)이 *"abandoned review backlog cleared before the free-plan run cap"*. 동시 런 상한은
`review_ready` 를 일부러 센다(`run_caps` — 무료 플랜 가격 레버). 그런데 그 적체 대부분은 아무도 기다리지 않는 런이었다:

- Safe Mode `deny` 는 `rejected_approach` + 사유가 있을 때만 런을 다시 연다. 정리성(`queue_cleanup`) 거절, 사유 없는 거절은
  런을 건드리지 않았다
- `mark_expired`(90일 스윕)는 런을 아예 건드리지 않았다

→ 산출물은 끝났는데 런은 `review_ready` 로 영원히 남아 상한을 잡았다.

형님 결정(2026-10-06): **누수만 막는다.** 상한 정책은 그대로.

## 바뀐 것

- `SafeModeQueue._release_run_if_nothing_left` — 거절(재개하지 않는 경우)·만료 뒤, 그 런이
  - `review_ready` 이고(진행 중인 런은 건드리지 않음)
  - 다른 항목이 pending / extended / approved 로 남아 있지 않고
  - PR 머지를 기다리는 중(`awaiting_merge`, #1109)이 아니면
  → `move_run_status` 로 `cancelled` ("released: safe mode item … denied/expired — nothing left to deliver")
- 만료 스윕은 런의 워크스페이스 스코프 안에서 돈다(`workspace_session_scope`) — RLS 에 가려 조용히 no-op 되지 않는다

## 검증 (로컬)

- [x] RED → GREEN — `tests/workflow/test_a_run_with_nothing_left_to_deliver_is_released.py`
  - 정리성 거절 · 사유 없는 거절 · 만료 → 런 `cancelled` (RED 3)
  - 대조군: 사유 있는 거절은 다시 연다(`open`) · 다른 항목이 남아 있으면 대기 · 머지 대기는 그대로 · 진행 중(open/running)은 그대로
- [x] 전선 절단 5곳(거절 · 만료 · 남은 항목 · 머지 대기 · 런 상태), 각각 빨강 / 8 수집
- [x] ruff · mypy

## 배포 후 (prod)

워커 변경 없음.

- [ ] 다음에 Safe Mode 항목을 정리성으로 거절하면 그 런이 `cancelled` 가 되고 동시 런 상한 수가 줄어든다
- [ ] 이미 쌓여 있는 적체(배포 전에 거절·만료된 항목의 런)는 소급되지 않는다 — 형님 판단으로 한 번 정리 필요

## 남은 틈

- 이미 거절·만료된 항목의 런은 그대로다(소급 정리 안 함)
- 비제품 런이 배송(delivered) 뒤에도 `review_ready` 로 남는지는 이번에 보지 않았다 — 배송 성공은 `auto_resolve_run_on_delivery` 의 영역
