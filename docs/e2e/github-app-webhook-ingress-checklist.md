# E2E — GitHub App 웹훅이 도착할 곳이 생긴다

**대상 PR**: `POST /api/webhooks/github` (토큰 없음) + manifest 에 `issues` · `issue_comment`

> 📏 **배포 전 prod 실측 (2026-09-30, 읽기 전용)**
> * App manifest 가 등록하는 훅 주소 `{issuer}/api/webhooks/github` — prod 라우트 표에 **없음**
>   (`/api/webhooks/{connector}/{webhook_token}` 과 `/api/v1/connectors/{id}/webhook` 뿐)
> * manifest 기본 이벤트 `push, pull_request` — **issues 는 애초에 GitHub 을 떠나지 않았다**
> * `trigger_events` 의 github 행 마지막 수신 **2026-07-01**
> * `BSVibe/bsvibe-app` 레포 웹훅 0개 · 활성 github 계정 `be3514f6` 에 `BSVibe/bsvibe-app` 바인딩 있음

---

## 배포 전 — 코드 (완료)

- [x] `tests/api/test_github_app_webhook.py` 11개 — 실제 라우트 · 레지스트리 주입 · 저장된 행 확인
- [x] 전선 절단 6건, 전부 컴파일(매번 **11개 실행**), 전부 빨강

      | 절단 | 뒤집힌 것 |
      |---|---|
      | 서명 검증 끔 (`secret=None`) | 위조 · 무서명 2 |
      | 시크릿 없음 게이트 제거 | 404 2 |
      | `is_active` 필터 제거 | 비활성 계정 1 |
      | 바인딩 조건 제거 | 미바인딩 repo 1 |
      | 첫 계정만 | 두 워크스페이스 1 |
      | 라우팅 키 스탬프 제거 | 바인딩 착지 1 |

## 배포 후 — 형님이 해야 하는 것 (manifest 는 **App 생성 시점에만** 적용된다)

- [ ] GitHub → Settings → Developer settings → GitHub Apps → **bsvibe** → Permissions & events
  - [ ] Repository permissions → **Issues: Read-only**
  - [ ] Subscribe to events → **Issues**, **Issue comment**
  - [ ] Webhook URL 이 `https://<api host>/api/webhooks/github` 이고 Active 인지
- [ ] 설치 계정(BSVibe org)에서 **권한 변경 요청 승인** — 승인 전엔 새 이벤트가 안 온다
- [ ] 앱이 `BSVibe/bsvibe-app` 에 **설치돼 있는지** (Installed GitHub Apps → Repository access)

## 배포 후 — 검증

- [ ] App → Advanced → Recent Deliveries 에서 ping 재전송 → **202** (이전엔 404)
- [ ] 테스트 이슈 1개 오픈 → `trigger_events` 에 `source=github` 1행, `product_id` = BSVibe,
      payload 에 `connector_account_id` · `resource_id`
- [ ] 요청 1 · 런 1 (증식 없음 — RLS fail-closed 관찰과 같은 센서)
- [ ] 런 산출물이 Safe Mode 에 대기 → 승인 → PR → merge-watch
- [ ] 봇이 연 이슈/PR 은 트리거 안 됨 (파서의 `sender.type == "Bot"` 스킵) — 자기 PR 로 루프 안 도는지
