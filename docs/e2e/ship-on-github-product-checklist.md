# E2E — GitHub 제품에서 "승인하고 출시"는 PR 로 배송된다 (#1112)

`ship`(`verification_failed` / `human_review_required` Decision, PWA 전용)은 산출물을 만들고, 로컬 제품 main 에 강제 머지하고,
런을 `shipped` 로 만들었다. `delivery_events` 행은 쓰지 않았다. GitHub 제품이면 그게 배송 전부라서

- DeliveryWorker 가 볼 행이 없어 **PR 이 열리지 않았다**
- 런은 `shipped` — #1109(머지 후에만 shipped) 위반
- 로컬 main 만 GitHub main 과 갈라졌다

형님 결정(2026-10-06): **ship 클릭이 곧 승인** — Safe Mode 카드를 한 번 더 띄우지 않는다.

## 바뀐 것

- `checkpoint_resolution._ship_decision_run` — 제품에 GitHub 바인딩이 잡히면(`resolve_github_binding`)
  - 산출물 + **delivery event**(`founder_approved: true`, producer `workflow:checkpoint_ship`)
  - 런 브랜치에 커밋은 하되 **로컬 main 강제 머지·워크트리 제거는 안 한다** (PR 이 그 브랜치에서 나간다)
  - 런은 `review_ready` 에 남는다 → 배송(PR) → 머지 감시 → 머지 후 `shipped`
- `DeliveryWorker` — `founder_approved` 이벤트는 Safe Mode 게이트를 건너 바로 dispatch
- 로컬 저장소 제품·비제품 런은 그대로 (강제 머지 → `shipped`)
- docstring 의 존재하지 않는 `ProofState.VERIFIED` → `PROVED`

## 검증 (로컬)

- [x] RED → GREEN — `tests/workflow/test_ship_on_a_github_product_opens_its_pr.py` (RED 5, 대조군 2)
- [x] 전선 절단 6곳(이벤트 발행 · 강제 머지 분기 · shipped 생략 · 바인딩 판정 · 게이트 우회 · 승인 표시), 각각 빨강 / 7 수집
- [x] lint-imports 6 kept · ruff · mypy

## 배포 후 (prod)

- [ ] 다음 BSVibe 런의 `verification_failed` / `human_review_required` 에서 PWA "승인하고 출시" → 런 `review_ready`, Safe Mode 카드 없음
- [ ] 워커 로그 `delivery_dispatched` → `BSVibe/bsvibe-app` 에 PR 이 열린다
- [ ] PR 머지 → 런 `shipped` (#1109 머지 감시)

## 남은 틈

- 이미 산출물이 있는 런(부분 산출물 등)에 ship 하면 새 이벤트를 쓰지 않는다 — 그 산출물의 기존 이벤트(또는 Safe Mode 항목)가 배송을 맡는다
- 배포 전에 ship 으로 `shipped` 가 된 GitHub 런은 소급되지 않는다 (PR 없음)
