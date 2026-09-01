# E2E — 인증된 셸 (라이브 스위트)

`apps/pwa/e2e-live/`. **CI 에서 안 돈다** — docker 스택과 실제 SSO 계정이 필요하다.
CI 가 도는 `apps/pwa/e2e/` 는 백엔드를 **도달 불가능한 주소**로 고정하므로 구조적으로
인증 이전 표면과 게이트의 리다이렉트만 판정한다. 이 문서는 그 **반대편 절반**이다.

## 왜 별도 스택인가 — 8700 은 이 머신에서 **prod** 다

`deploy/compose.yaml` 은 로컬 스택의 백엔드를 **8700** 에 publish 한다고 문서화한다.
그런데 형님 맥에서 그 포트는 **프로덕션 스택**이 이미 잡고 있다:

```
$ docker compose ls
bsvibe-prod   running(6)   …   0.0.0.0:8700->8000/tcp
```

⇒ 이 머신에서 "로컬 백엔드"와 "prod 백엔드"는 **둘 다 루프백에 있다.** `127.0.0.1`
을 근거로 안전하다고 판단하는 스위트는 실제 계정으로 **프로덕션에 로그인**한다.
**루프백은 증거가 아니다.**

그래서 가드가 두 겹이다:

| 층 | 언제 | 무엇을 |
|---|---|---|
| `assertLoopbackBackend` | config 로드 시점 (빌드 **전**) | 원격 백엔드 거부 |
| `assertIsE2EStack` | `global-setup` (브라우저 **전**) | `/api/health` 의 `git_sha` 가 `e2e-live-stack` 인지 — 일회용 스택을 **적극 식별** |

포트/sha 로 prod 를 **열거하지 않는다**(그 목록은 배포마다 썩는다). 오버레이만
찍는 마커로 **의도한 스택임을 증명**하고, 나머지는 전부 fail-closed.

## 실행

```bash
cd /Users/blasin/Works/bsvibe-app/main        # 또는 워크트리
export BSVIBE_E2E_KMS_KEY_B64=$(openssl rand -base64 32)
set -a; . ./deploy/.env.prod; set +a          # Supabase IdP 설정 (비밀 아님: URL·publishable·JWKS)
docker compose -f deploy/compose.yaml -f deploy/compose.e2e-live.yaml up -d postgres redis backend

export BSVIBE_E2E_EMAIL=admin@bsvibe.dev
export BSVIBE_E2E_PASSWORD=…                  # 메모리 reference_bsnexus_e2e_creds
cd apps/pwa && pnpm test:e2e:live
```

정리: `docker compose -p bsvibe-e2e-live down -v`

## 체크리스트

- [x] 게이트 열림 — 실제 폼 로그인 → `/brief` 에 앱 셸(`navigation`)이 뜬다
- [x] 세션이 **새로고침**을 넘어 산다 (jsdom 이 구조적으로 못 보는 명제)
- [x] 로그인 상태로 `/knowledge` · `/products` · `/settings` 직접 진입 통과
- [x] 인증된 화면에서 하이드레이션 치명 오류 0 — 네트워크 오류도 **필터하지 않는다**
      (백엔드가 진짜로 붙어 있으므로 실패한 fetch 는 노이즈가 아니라 발견이다)
- [x] 로그아웃이 세션을 **비운다** — 이후 `/brief` 가 다시 `/login` 으로 튕긴다
      (클릭 직후 URL 이 아니라 **다시 시도**로 판정)
- [x] 가드가 prod 를 거부한다 (아래 실증)

## 실증 (2026-09-01)

**초록** — `pnpm test:e2e:live` → **7 passed (15.4s)**

**빨강 1 — 인증의 절반만 배선했을 때 (조작 아님, 실제로 이렇게 처음 실패했다)**

`BSVIBE_SUPABASE_*`(발급)만 주고 `USER_JWT_*`(검증)를 빠뜨린 스택에서 **7개 전부 실패**:

```
RESP 200 http://127.0.0.1:8710/api/auth/login
RESP 401 http://127.0.0.1:8710/api/v1/account
RESP 200 http://127.0.0.1:8710/api/auth/refresh
RESP 401 http://127.0.0.1:8710/api/v1/account
→ /login 으로 되돌아감
```

⇒ **인증은 절반씩 두 군데에 설정된다.** 발급만 있으면 스택은 거의 맞아 보이는데
쓸모가 없다 — 로그인은 200 이고 그 뒤 모든 호출이 401 이라 로그인 즉시 튕긴다.
`deploy/compose.e2e-live.yaml` 이 네 개를 같이 싣는 이유다.

**빨강 2 — 가드가 prod 를 거부한다 (전선을 끊어 실증)**

```
$ BSVIBE_E2E_BACKEND_URL=http://127.0.0.1:8700 pnpm test:e2e:live
Error: refusing to run the live E2E suite against http://127.0.0.1:8700 —
/api/health reports git_sha="b3661b4", not "e2e-live-stack".
```

테스트 **0개 실행** · `test-results/` 없음 · 브라우저 안 뜸 · 자격증명이 전선에
올라가지 않음. **루프백 검사는 통과했다** — 잡은 것은 마커 검사다. 이 스위트가
존재하는 이유가 정확히 이 한 줄이다.

**빨강 3 — 스택이 안 떠 있으면**

```
Error: the live E2E backend at http://127.0.0.1:8799 did not answer /api/health
(TypeError: fetch failed) … Start the disposable stack first: …
```

## 가드 (CI 가 지킨다)

스위트는 CI 에서 못 돌지만 **가드는 돈다** — `apps/pwa/test/e2e-live-guard.test.ts`
(vitest, 13개). 아무도 안 지키는 가드는 죽은 걸 아무도 모른다.

핵심 명제: *"프로덕션이 루프백에서 응답해도 거부한다"* — 실제 prod health 모양
(`git_sha: "b3661b4"`)을 넣어 거부를 단언한다.

## 안 하는 것

- **dev-login 우회 없음 · 토큰 주입 없음.** 헬퍼는 실제 폼에 타이핑하고 실제 버튼을
  누른다. 세션을 심어주는 헬퍼는 로그인 경로가 깨져도 전부 초록이 된다.
- **`next dev` 안 씀.** dev 에서는 게이트된 라우트의 하이드레이션이 안 끝나
  `RequireAuth` 의 effect 가 영영 안 돈다 (인증 이전 스위트가 2026-08-31 에 실측).
- **`storageState` 공유 안 함.** 매 테스트가 콜드 브라우저에서 새로 로그인한다 —
  가장 검증할 값이 큰 것이 "로그인이 반복해서 된다" 이므로.
