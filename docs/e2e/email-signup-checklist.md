# E2E — 이메일 회원가입 (#937 게이트2)

**대상 PR**: `SupabaseAuthClient.sign_up` + `POST /api/auth/signup`
**전제**: 이 기능은 **닫힌 채로 배포된다.** Supabase 프로젝트의 이메일 가입이 꺼져 있어
GoTrue 가 *"Signups not allowed for this instance"* 로 거절하고, 라우트는 **403** 을 준다.

> 🚪 **문을 여는 것은 코드가 아니라 콘솔이다.** Supabase → Authentication → Providers →
> Email → *Enable email provider / Confirm email*. 이 PR 은 그 스위치가 켜질 때까지
> 아무 것도 노출하지 않는다.

> ⚠️ **#959 와 묶여 있다.** 감사가 *"공개 가입 전 RLS 재평가 — 배경 경로에 가드가 0이고
> fail-open 은 영구적"* 이라고 적어 뒀다. **스위치를 켜기 전에 #959 를 먼저 닫아라.**
> API 가 있다는 것과 문을 열어도 된다는 것은 다른 문제다.

---

## 단위 — 이미 통과 (8건)

- [x] `sign_up` 이 `/auth/v1/signup` 으로 email+password 를 POST 한다
- [x] 자동 확인 응답(토큰 있음) → 세션 반환, `confirmation_required=False`
- [x] **확인 필요 응답(user 가 최상위, 토큰 없음) → 세션 `None`, `confirmation_required=True`**
      ⭐ 이게 이 기능의 유일하게 까다로운 지점이다. 기존 `_session_from_gotrue` 는
      `body["user"]` 를 찾으므로 이 모양에서 *"missing user id"* 로 터진다 —
      **정상 가입이 에러로 보고된다**
- [x] GoTrue 4xx → `SupabaseAuthError`
- [x] 라우트: 자동 확인 → 200 + 세션 + **사용자 부트스트랩됨**
- [x] 라우트: 확인 대기 → 200 + `confirmation_required` + **행을 만들지 않는다**
- [x] 라우트: 가입 비활성 → **403 + 읽을 수 있는 detail**(500 아님)
- [x] 라우트: 외부 `redirect_to` → 400, **Supabase 까지 가지 않는다**
- [x] 양성 대조군: 허용된 `redirect_to` 는 그대로 전달된다
      (이게 없으면 위 테스트는 "redirect_to 를 통째로 무시한다"로도 통과한다)

### 전선 절단 3건 — 각각 **정확히 한 개씩** 빨개졌다
- [x] 확인 대기에도 부트스트랩 → `test_..._does_not_bootstrap_while_confirmation_is_pending`
- [x] `_validate_redirect_to` 제거 → `test_..._rejects_an_offsite_redirect`
- [x] 403 대신 예외 방류 → `test_..._surfaces_a_legible_refusal_when_signups_are_disabled`

## 배포 후 — 닫힌 문이 닫혀 있는지 (지금 걸 수 있는 유일한 칸)

- [ ] `curl -X POST https://api.bsvibe.dev/api/auth/signup -H 'Content-Type: application/json' \`
      `-d '{"email":"probe@invalid.example","password":"pw-12345678"}'` → **403**
      ⚠️ 500 이면 라우트가 에러를 방류하는 것이고, 200 이면 **문이 이미 열려 있는 것**이다
- [ ] 그 응답의 `detail` 이 사람이 읽을 수 있는 문장이다(스택트레이스 아님)

## §미실행 — 왜 못 걸었는지

체크 안 된 칸이 *"아직 안 봄"* 인지 *"못 봄"* 인지 구분되지 않으면 다음 사람이 같은 자리에서 또 멈춘다.

| 항목 | 왜 못 걸었나 | 대신 무엇이 지키나 |
|---|---|---|
| 실제 가입 → 확인 메일 수신 → 링크 클릭 → 세션 | **콘솔 스위치가 꺼져 있다.** 켜는 것은 형님 손이고, 켜기 전에 #959 가 먼저다 | 단위 8건 + 전선 절단 3건 |
| **PWA 가입 폼** | **아직 없다.** 이 PR 은 백엔드만이다 | 없음 — **다음 PR** |
| 이미 있는 이메일로 가입 시 동작 | GoTrue 설정에 따라 다르다(확인 메일 재발송 / 200 무응답). 스위치가 켜져야 실측된다 | 없음 — 켠 뒤 확인할 것 |
| 비밀번호 최소 길이 정책 | GoTrue 프로젝트 설정이 강제한다. 라우트는 빈 문자열만 막는다 | 켠 뒤 확인할 것 |

## ⚠️ 이 PR 이 남기는 상태 — 반쯤 배선됨

API 는 있고 **UI 는 없다.** 문이 닫혀 있어 사용자에게 노출되는 것은 없지만,
**다음 PR 로 PWA 폼이 오기 전까지 이 기능은 사람이 쓸 수 없다.** 그 사실을
여기 적어 두는 이유는, 켜진 줄 알고 콘솔 스위치만 올리면 **가입 경로가 없는
채로 문만 열리기** 때문이다.
