# E2E — BSVibe 가 여는 PR 은 무엇을 요청받았는지 말하고, 이름 붙인 이슈를 잇는다 (#1144)

실측 2026-10-07, PR #1142 (Direct 런으로 #1073 수정) 본문 전체: 제목 · `바뀐 파일 6개` · `검증: 8개 확인 통과`.
지시문의 원인·수용 기준은 리뷰어에게 안 닿았고 #1073 링크도 없었다 (이슈에서 시작된 런은 이미 `Closes #N`).
에이전트 산문은 일부러 본문에서 뺀다(스트리밍 독백) — 그러나 **형님 지시문**이 "왜"다.

형님 결정(2026-10-07): 지시문을 원문 그대로 본문에, 지시문의 `#N` 은 **닫지 않는** `Refs` 로. 닫기는 이슈에서 시작된 런만.

## 바뀐 것

- `_github_pr.with_founder_request` — 본문 뒤에 `**요청**`/`**Request**` + 지시문 인용(`> `, 제목 수준이 보고서를 넘지 않게) + `Refs #a, #b`(등장 순, 중복 제거)
- 이슈에서 시작된 런(`source_issue`)·지시문 없는 런은 본문 그대로
- `_open_pr_and_settle`(서버 · client_attach 공통) 이 PR 열기 직전에 적용. 지시문을 못 읽어도 PR 은 연다

## 검증 (로컬)

- [x] RED → GREEN — `tests/glue/test_pr_body_carries_the_founders_request.py` (RED 4 + 대조군 2, 실제 배송 경로 e2e 포함)
- [x] 전선 절단 4곳(호출 · 중복 제거 · 이슈 런 제외 · 인용) 각각 빨강
- [x] GitHub 배송 e2e · tests/delivery · 머지 후 shipped glue 144 passed · ruff · mypy · lint-imports

## 배포 후 (prod)

- [ ] 다음 Direct 런의 PR 본문에 `**요청**` 인용과 `Refs #N`, `Closes` 없음
