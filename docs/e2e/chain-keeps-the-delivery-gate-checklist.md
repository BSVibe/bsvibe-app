# E2E — 체인의 다음 스텝이 첫 스텝의 배송 게이트를 그대로 받는다 (#1108)

`AgentRunner.open_run` 은 Request 의 `binding_id`·`kind` 를 런 payload 로 복사하고, DeliveryWorker 는
바로 그 두 키로 산출물을 막는다.
- `binding_id` → 바인딩의 `output_mode`(`safe`)
- `kind == product_tick` → 자율 런은 Safe Mode 강제

`_maybe_spawn_next_step` 이 만드는 다음 런 payload 에는 둘 다 없었다. 그래서 둘째 스텝부터는
- 바인딩 `output_mode=safe` 가 안 걸렸다 — 워크스페이스 Safe Mode 가 꺼져 있으면 바로 배송
- product_tick 체인의 둘째 스텝이 강제 Safe Mode 를 벗어났다

## 바뀐 것

- 다음 스텝 payload 에 `binding_id`·`kind` 를 이어 싣는다(`agent_runner._delivery_gate_keys`) — 앞 런에
  있을 때만, 없던 키는 만들지 않는다

## 검증 (로컬)

- [x] RED → GREEN — `tests/execution/test_chain_keeps_the_delivery_gate.py`
  - DeliveryWorker 의 판정 함수(`_run_output_mode` · `_run_autonomous_origin`)를 둘째 스텝 런에 직접 돌린다 — 키가
    아니라 **게이트**가 살아남는지 본다
  - 각 테스트는 첫 스텝이 막혀 있는지부터 확인한다(대조군)
  - founder-direct 체인은 `kind` 를 새로 얻지 않는다(대조군)
- [x] 전선 절단: 이어 싣는 줄을 지우면 2 failed / 3 수집
- [x] `test_handoff_chain.py` 통과 · ruff · mypy

## 배포 후 (prod)

워커 변경 없음 — 체인 생성은 워커 컨테이너의 `AgentRunner` 에서 돈다(autodeploy).

- [ ] 단계 룰로 쪼개진 체인(`design` → `implement`)의 둘째 런 payload 에 `binding_id` 가 있고, 그 산출물이
  Safe Mode 큐에 들어간다(바인딩 `output_mode=safe` 일 때)
  - 자연 발생을 기다린다 — #1103 이후 같은 단계 쪼갬은 합쳐지므로 진짜 쪼갬은 드물다
  - payload 확인은 prod DB 조회가 필요하다 — 형님 쪽에서

## 남은 틈

- 체인 생성 외에 런을 새로 만드는 다른 경로(재시도로 새 런을 만드는 경로 등)가 같은 키를 잇는지는 이번에
  재지 않았다
