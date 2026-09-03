# E2E — PWA onboarding + honest worker status

Closes the audit's new-founder-can't-reach-first-value blocker. Philosophy: NOT
a managed worker — GUIDE the user to connect their OWN self-hosted worker (like a
GitHub Actions runner) and show honest status while none is connected.

## Behavior (unit-verified — Vitest/RTL)
- [x] `getBrief()` folds `listWorkers()` → `hasLiveWorker` (`heartbeat_fresh`) + `hasProducts` into `BriefView`; degrades to `[]`/false on a workers blip (never blanks the Brief).
- [x] Brief shows the 3-step OnboardingChecklist on a first-run workspace (0 products, 0 runs); hides once the workspace has products + a live worker.
- [x] Checklist marks a step done from the live signal (worker step ✓ when `hasLiveWorker`).
- [x] WorkingNow: an active run with NO live worker shows a calm "Waiting for a worker" pill + hint instead of the ever-climbing "Working" timer.
- [x] Request FAB: a 400 (zero-product workspace) shows the localized "Create a product first…" hint, not the generic send error (and NOT the raw English backend detail — keeps English off a KO surface).
- [x] Full PWA suite (710) green; tsc --noEmit clean; biome clean; ko/en at key parity.

## Live E2E (staging/prod PWA, a fresh workspace)

걸었다 — **2026-09-03**, 일회용 스택 `bsvibe-e2e-live`(빈 DB)에서 실제 브라우저로.
스펙 `apps/pwa/e2e-live/onboarding.spec.ts`. 최종 **12 passed**(인증 셸 7 + 온보딩 5).

⭐ **걸어보니 이 목록의 두 항목이 산출 불가능한 문장이었다.** 둘 다 프로덕션 결함이었고
둘 다 고쳤다. 유닛은 그동안 전부 초록이었다.

⚠️ 이 스위트는 `authenticated-shell.spec.ts` 와 달리 **상태를 가진다.** "첫 실행"은
한 번 벗어나면 못 돌아오는 상태라 제품을 만드는 검사가 **맨 뒤**이고, 스택이 안
비었으면 조용히 다른 워크스페이스를 판정하는 대신 **크게 실패**한다.
재실행 전에 반드시: `docker compose -p bsvibe-e2e-live down -v`

- [x] 0 제품 · 0 워커 → Brief 가 3단계 안내를 준다 (빈 화면이 아니라)
      ⚠️ **명제를 고쳐 적었다.** 원래 *"`All caught up` 이 아니라"* 였는데, 실측하니
      Working 섹션의 빈 상태 문구는 **안내 아래에 함께** 뜬다. 부재로 단언했더니
      멀쩡한 첫 실행에서 빨갛게 죽었다. 사용자가 받으면 안 되는 것은 "빈 상태가
      전부인 화면"이므로 검사는 **안내가 페이지를 앞선다**로 쓴다.
- [x] 🔧 워커 단계가 **워커를 실제로 연결할 수 있는 화면**으로 데려간다
      **결함이었다.** 링크가 `href="/settings"` 였는데 그건 **General 탭으로 서버
      리다이렉트**되고, `ExecutorWorkers`(등록·service install 표면)는
      `/settings/models` 에 있다. 새 사용자는 이 단계를 끝낼 수 없는 탭에 떨어졌다.
      → `href="/settings/models"` 로 수정.
      ⚠️ **이 검사는 한 번 운으로 통과했었다** — 첫 단언이 `/\/settings$/` 였고
      리다이렉트 전 URL 을 잡아서 초록이었다. 명제를 **목적지**(`/settings/models`
      + `Executor workers` 영역이 보인다)로 다시 썼다.
- [x] 제품 없이 요청 → **로컬라이즈된** "Create a product first, then send your request."
      (일반 전송 오류도, 원문 영어 백엔드 detail 도 아니다 — 400 이 여기로 매핑되는
      것을 증명하는 경로는 이것뿐이다)
- [x] 🔧 제품을 만들면 1단계가 ✓ 로 바뀌고 **남은 단계 안내가 살아 있다**
      **결함이었다.** `BriefContent` 의 `firstRun` 이 `!hasProducts` 를 요구해서,
      제품이 생기는 순간 체크리스트가 **통째로 사라졌다**. 그래서 1단계의 ✓ 는
      프로덕션이 도달할 수 없는 상태였고 — 더 나쁘게 — **워커가 아직 없는 사용자가
      제품을 만들면 남은 두 단계의 안내까지 함께 사라졌다.** 이 화면이 닫으려던
      *새 사용자가 첫 결과물에 못 닿는* 블로커의 절반이 그대로 남아 있었다.
      → `!(hasProducts && hasLiveWorker)` 로 수정. 이건 `OnboardingChecklist` 자신의
      docstring(*"the whole block hides once the workspace can actually produce"*)이
      처음부터 적어둔 조건이다 — 부모가 다른 것을 구현하고 있었다.
      ⚠️ **유닛이 왜 못 잡았나**: 기존 테스트가 `OnboardingChecklist` 를 **직접**
      렌더하며 `hasProducts` 를 손으로 넣었다. 부모가 절대 안 주는 조합이라
      초록인 채로 결함이 살았다. 새 유닛 테스트는 **`BriefContent` 를 통과**시킨다.
- [ ] ⏭ 워커 등록 + `bsvibe-worker service install` → `hasLiveWorker` 뒤집힘 → 런 완료
      **안 걸었다.** 호스트에 네 번째 워커 데몬을 설치해야 하고(일회용 스택을 향한
      데몬은 스택과 함께 죽는다), 런 완료까지 보려면 로그인된 코딩 에이전트 CLI 가
      필요하다. 별도 세션의 일이다.
- [x] KO 로케일이 해요체 문구를 보여주고 영어 크롬이 안 샌다
      (`locale: "ko-KR"` — 로케일은 Accept-Language 로 정해진다)

### 걸으면서 나온 하네스 결함 둘 (제품 아님)

1. 🚨 **`@playwright/test` 가 선언만 돼 있고 이 머신에 설치돼 있지 않았다.**
   `node_modules` 는 7/7 자이고 이 패키지는 9월에 추가됐다 — 빠진 devDep 이 13개 중
   **정확히 이것 하나**. ⇒ 야간 러너(`_infra/scripts/e2e-live-nightly.sh`)의 라이브
   절반은 형님이 Keychain 을 넣는 **바로 그 순간** `playwright: command not found` 로
   죽었을 것이다. SKIP 경로만 실증돼 있어서 아무도 몰랐다.
   ⇒ **사람을 기다리는 절반은, 사람이 오기 전에 한 번은 강제로 돌려봐야 한다.**
2. **`signIn` 헬퍼가 영어 전용이었다.** `getByLabel(/email/i)` · `"Password"` ·
   `/continue$/i` — 로케일은 Accept-Language 로 정해지므로 `ko-KR` 테스트가
   **`signIn` 안에서 30초 타임아웃**으로 죽었고, 그 실패는 *"한국어 온보딩이 깨졌다"*
   처럼 읽혔다. 제품은 멀쩡했다. 폼의 `#email` / `#password` /
   `button[type="submit"]` 로 바꿨다 — 여전히 진짜 폼에 타이핑하고 진짜 버튼을 누른다.

### 게이트

vitest **757 passed** (112 files) · `tsc --noEmit` clean · biome clean ·
live E2E **12 passed** (fresh stack)
