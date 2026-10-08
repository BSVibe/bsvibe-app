# E2E — 형님이 direct 로 만든 스케줄은 승인 카드 없이 전송된다 (#1072)

실측 2026-09-29: 형님이 직접 만든 주간 BStockReport 스케줄(`instruction`)의 딜리버러블이 **매주** Safe Mode 대기열에 들어갔다
(`a65412ad` → item `d0779e47`). 게이트 1번 규칙이 워크스페이스 Safe Mode 라 항상 이겼다. 선택지는 "워크스페이스 전체 Safe Mode 끄기"
또는 "매주 승인" 뿐이었다.

형님 결정(2026-10-08): 백엔드 + MCP + PWA 토글, 주간 리포트는 direct.

## 바뀐 것

- 스케줄 `payload.output_mode` = `safe`(기본) | `direct` — 마이그레이션 없음. `product_tick` 은 `safe` 만(PT3)
- 저작: REST `POST`·MCP `bsvibe_schedules_create` 에 `output_mode`; 변경: REST `PATCH {output_mode}` · MCP `bsvibe_schedules_set_output_mode`
- 발화: instruction 스케줄이 direct 면 트리거 payload 에 `schedule_output_mode=direct` → Request → `open_run` 이 런 payload 로
  (다음 스텝도 `_delivery_gate_keys` 로 유지)
- 게이트: `autonomous_origin`(PT3) → 큐 / `schedule_direct` → 바로 전송 / 그다음 워크스페이스 Safe Mode · 바인딩 output_mode
- PWA 스케줄 탭: 생성 폼 "승인 없이 전송" 체크 + 행마다 토글(instruction 만), 낙관적 갱신·실패 시 되돌림. ko/en 문구

## 검증 (로컬)

- [x] RED → GREEN — `tests/workflow/test_a_direct_schedule_delivers_without_approval.py` (14: 저작·변경·MCP·발화·런·게이트·DeliveryWorker 실주행)
- [x] PWA — `test/schedules-direct.test.tsx` (4) · `schedules-client.test.ts` 에 PATCH/POST 계약 2개
- [x] 백엔드 전선 절단 8곳 각각 빨강 (워커 전달 · open_run 전파는 실주행 테스트 추가 후)
- [x] PWA 전선 절단 3곳 빨강 (클라이언트 전송은 로컬 localStorage 환경 문제로 client 테스트가 못 돌아 CI 에서 확인)
- [x] 기존 형태 단언 4개 재진술(payload 에 `output_mode: "safe"`, PATCH 필드)
- [x] 백엔드 2963 passed · ruff · mypy · lint-imports · tsc · biome

## 배포 후 (prod)

- [ ] BStockReport 주간 스케줄 `534f78a7` → `direct`
- [ ] 10-12 주간 런의 딜리버러블이 Safe Mode 큐 없이 텔레그램으로 (로그 `delivery_dispatched`, `safe_mode_enqueued` 없음)
