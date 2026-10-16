# Paddle 구독 웹훅 → 워크스페이스 플랜 (#928 2단계)

> ⏸ **보류 (형님, 2026-10-16)** — 사업자 등 이슈로 결제는 당분간 안 한다. 무료 베타로 연다.
> 이 웹훅은 시크릿이 비어 있는 동안 404 를 답하며 **휴면** 상태다. 아래 "형님 — Paddle 설정"은 재개할 때.

`POST /api/webhooks/paddle` 이 `Paddle-Signature` 를 검증한 뒤 `apply_plan` 으로 워크스페이스를 옮긴다:
active/trialing + Pro 가격 → Pro, canceled/paused → Free, past_due → 그대로.
백엔드만 — 자동 배포. 마이그레이션 `workspace_billing`(추가만).

## 유닛 / 배선 (로컬)

- [x] 서명된 activation → Pro (10 런 · 2 슬롯), 구독 · 고객 id 저장
- [x] 위조 / 만료 / 본문 변조 → 401, 아무것도 안 바뀜. 시크릿 없음 → 404
- [x] 해지 → Free, past_due → 플랜 유지 + 상태 기록, 모르는 가격 → 변경 없음
- [x] 운영자 워크스페이스는 결제로 안 바뀐다. 한 번 묶인 구독을 이후 이벤트가 다른 워크스페이스로 옮기지 못한다
- [x] 모르는 워크스페이스 / 워크스페이스 없음 → 200, 변경 없음 (재시도로 고칠 수 없다)
- [x] 같은 이벤트 id 재전송은 한 번만 적용, 늦게 도착한 옛 이벤트는 무시
- [x] 전선 절단 10곳 — 일회용 PG(fail-closed RLS, `bsvibe_app` 역할)에서 빨강, 교차 테넌트 읽기와 스코프 쓰기 포함

## 형님 — Paddle 설정 (재개할 때, 샌드박스 먼저)

1. Paddle 샌드박스 계정 → **Catalog**: 상품 "BSVibe Pro", 가격 월(+ 연)
2. **Developer tools → Notifications → New destination**
   - URL: `https://api.bsvibe.dev/api/webhooks/paddle`
   - 이벤트: `subscription.created`, `subscription.activated`, `subscription.updated`,
     `subscription.canceled`, `subscription.paused`, `subscription.resumed`, `subscription.past_due`
3. `.env.prod` (절대 커밋하지 말 것):
   - `BSVIBE_PADDLE_WEBHOOK_SECRET=<destination 시크릿 키>`
   - `BSVIBE_PADDLE_PRO_PRICE_IDS=pri_…,pri_…`
   - 백엔드 재생성 (다음 머지 때 자동 배포가 하거나, `docker compose up -d --force-recreate backend`)

## prod (샌드박스) 실측 — 재개 후

- [ ] 틀린 시크릿을 둔 채 Paddle "Send test event" → 백엔드 로그 `paddle_webhook_signature_rejected`
- [ ] `customData: {workspace_id: <테스트 워크스페이스>}` 로 샌드박스 결제 → 로그 `paddle_webhook_applied plan=pro`,
      그 워크스페이스가 4번째 동시 런을 받는다
- [ ] 샌드박스 구독 해지 → `plan=free`
- [ ] 3단계(PWA 결제창)가 나오기 전까지는 결제 시 `customData.workspace_id` 를 손으로 넣어야 한다
