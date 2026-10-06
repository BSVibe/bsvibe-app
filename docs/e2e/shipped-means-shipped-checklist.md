# E2E — "배송됐어요"는 런이 배송될 때, 승인 카드는 따로 (#1111)

`shipped` 알림은 **검증 시점**(`write_verified_deliverable`)에 나갔다. 그때 런은 `review_ready` 이고 Safe Mode 가 켜져 있으면
형님 승인 전이다. 그리고 그 알림이 사실 텔레그램·슬랙·디스코드의 **승인/거절 버튼 카드**였다 — 제목만 "작업 완료"/"Done".
거절하면 "완료" 알림을 받은 일이 끝내 배송되지 않았다. 진짜 배송(머지·로컬 출시) 시점엔 아무 알림도 없었다.

형님 결정(2026-10-05): **이벤트를 둘로 분리.**

## 바뀐 것

| 이벤트 | 언제 | 제목(ko / en) | 버튼 |
|---|---|---|---|
| `review_ready` (신규) | 검증 통과, 산출물 기록 시 | 검토할 결과가 나왔어요 / Ready for your review | 승인 · 거절 |
| `shipped` | 런이 실제로 `shipped` 가 될 때 — `run_status.move_run_status` (모든 shipped 이동의 유일한 자리) | 배송됐어요 / Shipped | 없음 |

- `shipped` 는 런당 한 번(`shipped:<run_id>`), 본문은 런의 최신 산출물 제목 줄, 링크는 그 산출물. 새 producer `workflow:run_status` 등록
- **설정 이행:** 분리 이전에 저장된 매트릭스엔 `review_ready` 키가 없다. "없음 = 꺼짐"이면 승인 카드가 조용히 끊긴다 →
  `effective_switch` 가 **`shipped` 스위치를 이어받게** 한다. 알림 게이트와 설정 화면(REST·MCP view)이 같은 함수를 읽는다
- **검증 완화:** 매트릭스 키는 "정확히 같음"이 아니라 "알려진 이벤트의 부분집합". 이전 이벤트 목록을 든 PWA 가 저장할 수 있다
  (모르는 키·중첩 값·bool 아닌 값은 여전히 422)
- `DEFAULT_ON_EVENTS` 를 notifications leaf 로 옮겼다(뷰와 게이트가 한 규칙)
- PWA: 알림 설정에 `review_ready` 스위치("검토할 결과" / "Ready for review")

## 검증 (로컬)

- [x] RED → GREEN
  - `tests/notifications/test_shipped_means_shipped.py` — shipped 이동만 `shipped` 알림 · 다른 이동·거절된 이동은 없음 · 버튼은
    `review_ready` 에만 · 기본 켜짐 · 옛 매트릭스는 `shipped` 를 이어받음 · 명시값 우선 · 설정 뷰가 게이트와 같다
  - `tests/api/test_v1_notifications.py::test_a_matrix_saved_before_review_ready_still_saves_and_reads_whole`
- [x] 기존 명제 재진술(형님 결정으로 명제가 바뀜): `test_shipped_producer.py` → `test_review_ready_producer.py`, 빌더·키보드·카피 테스트의
  승인 카드 이벤트를 `review_ready` 로, `shipped` 카피는 새 제목으로
- [x] ⚠️ 구현 중 발견: `PrefsView` 가 매트릭스 키를 **정확히** 요구해서, 이벤트 하나 추가가 모든 기존 워크스페이스의 `GET /prefs` 를 깨뜨릴 뻔했다
- [x] 전선 절단 4곳(배송 알림 · 검증 시점 이벤트 · 상속 · 뷰), 각각 빨강 / 27 수집
- [x] PWA `notifications-tab` 8 통과. `settings-tab-content` 의 `localStorage.removeItem` 실패는 **수정 전 main 에서도 같다**(로컬 환경) — CI `pwa` 가 판정
- [x] ruff · mypy · import-linter 6/6

## 배포 후 (prod)

**프런트·백엔드가 같이 바뀐다** — Vercel 은 머지 즉시, 백엔드는 2분 안. 그 사이 새 PWA 가 옛 백엔드에 7키 매트릭스를 PUT 하면 옛 검증
("정확히 6키")이 422 를 낸다 — 알림 설정 저장만 2분 정도 막힌다. 읽기는 영향 없음.

- [ ] 다음 검증 통과 런: 텔레그램에 "검토할 결과가 나왔어요" + 승인/거절 버튼
- [ ] 그 런이 shipped 되면(로컬 출시 또는 PR 머지) "배송됐어요", 버튼 없음
- [ ] PWA 알림 설정에 "검토할 결과" 스위치가 형님의 `shipped` 설정과 같은 값으로 보인다

## 남은 틈

- 로컬 제품 런은 검증 직후 곧바로 자동 출시되므로 "검토할 결과"와 "배송됐어요"가 몇 초 간격으로 둘 다 온다. 그 카드의 승인 버튼은
  누를 것이 없다 — 이전에도 같았다(옛 `shipped` 카드에도 버튼이 있었다)
- 일일 요약의 "shipped N건"은 `updated_at` 기준 shipped 런 수 그대로다
