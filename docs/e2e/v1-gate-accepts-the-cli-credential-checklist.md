# E2E — v1 게이트가 CLI 크리덴셜을 받는다 (#1017)

**대상 PR**: v1 인증 게이트를 **이중 발급자**로. `bsvibe login` 이 저장하는
임베디드 OAuth ES256 액세스 토큰이 `/api/v1/*` 에 도달한다.
**전제**: prod 는 autodeploy. 배포 후 **컨테이너 재생성**으로 확인.
호스트 워커는 이 변경과 무관하다(워커는 `/mcp` 를 쓴다).

> 🧭 **이건 설정 문제가 아니다.** `USER_JWT_JWKS_URL` 을 api.bsvibe.dev 로 돌리면
> PWA 가 죽고, CLI 는 그래도 못 들어온다 — 액세스 토큰의 `sub` 는 `UserRow.id` 이지
> `supabase_user_id` 가 아니다. **발급자가 둘인데 설정 슬롯은 하나**다.

---

## 사전 측정 — 전제가 맞는지 (코드로 확인)

- [x] `~/.config/bsvibe/credentials.json` 의 `access_token` 이 **어느 클래스인가**
      → `alg=ES256` · `iss=https://api.bsvibe.dev` · `aud=bsvibe-app` · `sub`=UUID · **`wsp` 클레임 보유**
      ⇒ Supabase 세션 JWT 가 **아니다**. 우리 임베디드 OAuth 액세스 토큰이다
- [x] v1 게이트가 검증기를 **몇 개** 쓰는가 → **하나** (`shared/authz/auth.verify_user_jwt`, Supabase 전용).
      진입점은 `backend/api/deps.py` **한 줄**(`from backend.shared.authz.deps import get_current_user`)
- [x] 이 한계가 이미 알려져 있었는가 → **그렇다.** `backend/api/main.py` 주석:
      *"Mounted outside the auth-gated v1 router because that gate accepts only a Supabase session JWT"*
      ⇒ PAT 라우터 **하나만** 게이트 밖으로 빼서 우회했고 나머지 v1 은 그대로 막혀 있었다
- [x] 해법 부품이 이미 있는가 → **있다.** `backend/api/pat_auth.py` 가 정확히 그 이중 발급자
      리졸버다(`iss` 로 결정적 분기 · run 토큰 거절 · 스코프 게이트 · workspace GUC 발행)
- [x] 세 검증기가 **같은 신뢰 집합**인가 → **아니다.**
      `workers_register_auth._try_mcp_access_token` 은 `row.expires_at` 을 **안 본다**
      (`mcp/auth.resolve_principal_from_bearer` 는 본다) ⇒ 행 만료된 PAT 로 워커 등록이 된다

## 유닛 — `tests/api/test_v1_dual_issuer_auth.py` (진짜 토큰, 진짜 리졸버)

> 인증은 **오버라이드하지 않는다.** DB 세션만 테스트 DB 로 돌린다.
> 기존 스위트가 prod 장애를 못 잡은 이유가 바로 **자기가 서명한 토큰**이기 때문이다.

- [x] `issue_token_pair` 가 내준 CLI 토큰으로 `GET /api/v1/products` → **200** (RED: 401)
- [x] Supabase 세션 JWT 로 같은 호출 → **200** (PWA 회귀 가드)
- [x] 베어러 없음 → 401 · 쓰레기 베어러 → 401(500 아님)
- [x] `issue_run_task_token` 의 런 스코프 토큰 → **403** (디스패치된 에이전트의 90분
      크리덴셜이 워크스페이스 전체 발판이 되면 안 된다)
- [x] `mcp:read` 만 든 토큰으로 `PATCH` → **403** (`mcp:write` 요구)
- [x] ⭐ **양성 대조군**: `mcp:write` 있으면 같은 `PATCH` 가 **404** — 403 이 스코프
      때문임을 증명한다(핸들러까지 도달했다는 뜻)
- [x] revoke 된 토큰 → 401 · **행 만료**(JWT `exp` 는 아직 살아 있음) → 401
- [x] ⭐ **격리**: 워크스페이스 둘에 속한 유저가 **ws-B 토큰**을 내면 `GET /api/v1/workspace`
      가 **ws-B** 를 답한다 — 멤버십 순서(ws-A)가 아니라 **토큰의 `wsp`**
- [x] 떠난 워크스페이스의 토큰 → **403** (멤버십 상실이 토큰 만료를 기다리지 않는다)

## 전선 절단 (가드가 진짜 그 홉에 있는가)

- [x] 이중 발급자 분기를 되돌리면 → `test_cli_access_token_reaches_a_v1_route` **빨강**
- [x] run 토큰 거절 한 줄을 지우면 → `test_run_scoped_task_token_cannot_reach_v1` **빨강**
- [x] 스코프 게이트를 지우면 → `test_read_only_token_cannot_mutate` **빨강**,
      **양성 대조군은 초록 유지**(404)
- [x] `wsp` 대신 멤버십으로 워크스페이스를 풀면 → 격리 테스트 **빨강**
- [x] 각 절단이 **컴파일은 된다**(문법 오류로 빨개진 게 아님) · **돌아간 테스트 개수**를 확인

> ✅ **2026-09-21 머지 전 로컬 실측.** 유닛 11/11 · 전선 절단 5/5 가 **정확히 의도한
> 테스트만** 뒤집었고 스코프 게이트의 **양성 대조군(404)은 초록을 유지**했다. 다섯 절단 모두
> import 가 통과한다(문법 오류로 빨개진 게 아니다). 전체 스위트 **6416 passed** ·
> import-linter **6 kept / 0 broken** · mypy 깨끗.
> 절단 5(행 만료)는 **MCP 쪽 테스트까지** 같이 빨개졌다 — 검증기가 실제로 공유라는 증거다.

## 배포 후 — prod 실측 (형님 계정 불필요, 내 CLI 세션으로)

- [ ] 배포 확인: prod 커밋 SHA + **컨테이너 재생성 시각**
- [ ] `bsvibe refresh` → `bsvibe products list` **200** (이슈의 재현 명령 그대로)
- [ ] `bsvibe runs list` · `bsvibe deliverables list` 도 200
- [ ] **PWA 가 멀쩡하다** — 로그인 상태로 제품 목록/런 목록이 그대로 뜬다(회귀 가드의 진짜 표면)
- [ ] `/api/v1/oauth/pats` 가 여전히 200 (게이트 밖 라우터를 안 깨뜨렸다)
- [ ] `/mcp` 가 여전히 붙는다 (같은 토큰 클래스, 다른 표면)

## 안 잰 것 / 이월

- [ ] `https://api.bsvibe.dev/oauth/jwks` 가 **빈 키셋**을 주는 것 — 이 레포에 그 라우트가
      **없다**. `auth.bsvibe.dev`(bsvibe-site) 쪽으로 보인다. 별건
- [ ] `workers_register_auth` 의 행-만료 누락은 이 PR 에서 같이 고친다 — 다만
      **prod 에 만료된 행이 실제로 있는지**는 안 셌다
- [ ] PAT 라우터를 다시 v1 게이트 **안으로** 옮기는 것(우회가 더 이상 필요 없다) — 라우터
      레벨 의존성이 라우트 인증을 선점하는 함정이 있어 별건으로 둔다
