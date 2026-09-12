# BSVibe 세션 인수인계 — 2026-09-11

**prod**: `f0629ba` (배포·실행검증 완료) · **열린 PR**: 없음 · **워크트리**: 없음
**워커**: 호스트 3개 — 배포마다 `launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}`

이 세션은 인수인계(09-10)의 **게이트 1 후속**으로 시작해 PR **#914~#925 열두 개**를 냈고,
도중에 **감사 문서가 통째로 놓친 축**(제품 × 커넥터 라우팅)을 찾아 세 경로를 다 닫았다.

⚠️ **§Ⅳ 를 먼저 읽어라** — 이 세션에서 내 진단이 **여섯 번** 틀렸고, 그중 셋은 하루 안에
자체 반증됐다.

---

## §Ⅰ — 머지·배포된 것 (전부 prod 실행검증)

### 게이트 1 후속 · 스케일
| PR | 내용 |
|---|---|
| #914 | **워커 실행 런의 토큰 계측 체인 복원.** #911 의 상한이 네이티브 LiteLLM 턴에만 배선돼 있어 **워커 런은 영원히 0 토큰** → 상한이 발화 불가였다. 체인 네 링크(CLI usage 파싱 · `executor_tasks` 컬럼 · `/workers/result` 필드 · `ChatResponse` 생성)를 다 이었다. prod 실증: 런 미터 = executor 태스크 usage 의 **정확한 합**(43,187/658) |
| #915 | 위 배포 후 검증 기록 |
| #916 | **워커 디스패치 스트림 유계.** prod Redis 66MB 중 **57.6MB(87%)가 트림된 적 없는 스트림**. `maxlen` + revoke 시 삭제. **38.4MB 회수** |
| #917 | **verify slot 2층** — 전역 디스크 바운드 + 워크스페이스 플랜 티어. ⚠감사의 처방("슬롯 키에 워크스페이스 축")을 쓰면 **컴포즈 프로젝트명이 전역이라 테넌트끼리 서로의 살아있는 스택을 철거**한다 |

### 게이트 3 (가용성)
| PR | 내용 |
|---|---|
| #918 | **박스 밖 가용성 감시**(GitHub Actions). ⚠`runs-on: ubuntu-latest` 가 하중 — self-hosted 러너를 쓰면 감시자가 감시 대상과 함께 죽는다 |
| #919 | **감시가 어느 층이 깨졌는지 말한다** — `OK`/`DEPENDENCY`/`APP`/`EDGE`/`UNREACHABLE`. `/api/health` 는 settings 만 반환해 **Supabase 정지 내내 200** 이었다 |
| `_infra` ×2 | `heartbeat.sh` 의 헬스 경로 수정(`/api/v1/health` 는 **존재하지 않는다**) + 같은 깊은 프로브 |

### 텔레그램 / 채팅
| PR | 내용 |
|---|---|
| #920 | **텔레그램 웹훅을 제품이 등록한다.** `getWebhookInfo` → `url_set: False` 였고 `setWebhook` 호출이 코드베이스에 **0개**였다. ⚠URL 만 등록하면 안 고쳐진다 — 리졸버가 시크릿 없으면 **봇 토큰**으로 폴백하는데 `:` 때문에 텔레그램이 절대 못 돌려준다 |
| #921 | **`needs_you` 를 채팅에서 바로 답한다.** 버튼은 `shipped` 에만 있었다. inbound 는 **큐에 적고** 엔진(AgentWorker 틱)이 `resolve_checkpoint` 로 적용 — R2c 계약을 지키면서 |

### 제품 × 커넥터 축 (감사가 놓친 것)
| PR | 내용 |
|---|---|
| #922 | **인바운드**: `(connector_account_id, resource_id)` → binding → product |
| #923 | **알림**: `product_id` 를 싣고 카드에 `[제품명]`. 채널 좁히기(**폴백함**) |
| #924 | **`trigger.enabled` 삭제** — 소비자 0인 노브 |
| #925 | **배송**: 제품 범위 라우팅(**폴백 안 함**) |

---

## §Ⅱ — 이 세션의 중심 발견: 제품 × 커넥터 축

`resource_bindings` 는 독스트링 첫 줄이 **"Per-Product × Connector 3-knob binding"** 이고
인덱스·헬퍼·MCP 툴·**형님 데이터**까지 다 있었는데 **라우팅으로 읽는 코드가 0** 이었다.

**제품이 하나일 때는 워크스페이스 = 제품이라 증상이 0이다. 둘이 된 순간 어긋난다.**
감사가 이 축을 못 본 이유도 같다 — 단일 제품 관점에서는 모든 경로가 맞게 "보인다".

### ⚠️ 폴백 정책이 두 경로에서 **반대**다 — 이게 이번 작업의 핵심 판단

| | 바인딩 없을 때 | 왜 |
|---|---|---|
| **알림**(#923) | 워크스페이스 전 채널로 **폴백** | 알림을 **잃는 게** 공유 채널로 오는 것보다 나쁘다 |
| **배송**(#925) | **아무 데도 안 감** | 엉뚱한 데 쓴 산출물은 **외부로 나가고 되돌리기 어렵다** |

github 독스트링이 후자의 원칙을 이미 적어 뒀다: *"None 은 의도된 안전한 결과이며, 제품이
소유하지 않은 repo 에 쓰는 것보다 낫다."* ⇒ **좁히기를 구현할 때 "못 받는 비용"과 "잘못
가는 비용"을 견줘라. 함수 모양이 같아도 정책이 반대일 수 있다.**

---

## §Ⅲ — 형님이 해소한 것 / 형님 잔여

### ✅ 이 세션에 해소됨
* **Bot Fight Mode** — `api.bsvibe.dev` 가 **데이터센터 IP 전체에서 403**(`cf-mitigated: challenge`)
  이었다. 클라우드 VM 워커가 **등록조차 불가**였고(게이트 2 다중 사용자 차단),
  `curl | sh` 온보딩도 403. 형님이 끄자 전 표면이 뚫렸다.
  ⚠**주거용 IP 의 200 을 "공개적으로 접근 가능"으로 읽지 마라.** 코드에 없는 결함이라
  어떤 테스트로도 안 잡힌다.
* **Supabase 정지** — 복구됨. 박스 밖 감시가 이제 `DEPENDENCY` 로 잡는다(#919).

### 형님 잔여
1. **healthchecks.io ping URL** → `~/.bsvibe/heartbeat.env` 의 `HEARTBEAT_PING_URL=`.
   배관은 **다섯 방향 실증 완료**(정상→ping, Supabase 정지→중단, 앱 사라짐→중단, …).
   지금은 파일 자체가 없어 inert.
2. **텔레그램 어느 방을 쓸지** — 바인딩이 **1:1 채팅**(`8242700007`)을 가리킨다.
   그룹방(`-1003257931284`)에서 보내면 제품에 안 붙는다. 설정이지 결함 아님.
3. **BSVibe 배송 바인딩** — #925 이후 BSVibe 산출물은 커넥터 배송이 안 나간다
   (github 은 별도 경로라 영향 없음). 필요하면 바인딩 추가.
4. **재부팅 cold-boot 테스트**(colima-ensure) · **Supabase 이메일 가입 정책** — 09-10 이월.
5. **prod 바인딩 JSON 의 `"enabled": false` 잔해** — 무해하나 잔해. 데이터 마이그레이션 여부 판단.

---

## §Ⅳ — ⚠️ 이 세션에서 내 진단이 여섯 번 틀렸다

**셋은 "구멍이 아니라 의도된 설계"였다** — 지목된 대로 구현했으면 멀쩡한 걸 부쉈다:
1. **run cap 을 웹훅/스케줄에도** → `run_caps.py` 독스트링이 그 부재를 **근거와 대안까지
   달아 변호**한다. 그렇게 태어난 런도 `count_held_runs` 가 세서 다음 제출 예산을 소비한다.
2. **verify slot 키에 워크스페이스 축** → 컴포즈 프로젝트명도 전역이고 **그게 고아 회수의
   하중**이다. 키에만 넣으면 테넌트끼리 **서로의 살아있는 스택을 철거**한다.
3. **"알림이 워크스페이스 단위"가 의도인지** → 채널 선택은 의도(`Notifier N1a`), 다만
   *제품을 아예 모르는 것*은 갭이었다.

**셋은 "부재"가 아니라 "절반 구현/입력 부족"이었다**:
4. **"B10b 미배선"** → Receive 스테이지는 **존재하고 IntakeWorker 가 부른다**. 입력(페이로드의
   `connector_account_id`/`resource_id`)이 안 들어올 뿐. 주석이 그 상태를 이름까지 붙여 뒀다:
   *"an inbound parser that hasn't been retrofitted yet"*.
5. **"배송은 불리언 게이트"** → 절반만. **github 은 이미 제품 라우터**(#681·#684·#723).
6. **"고아 스트림 5개 = 9.7MB"** → 워크스페이스 범위 active 목록으로 재서 틀렸다.
   실제 회수는 **38.4MB**(4배).

⇒ **"X 가 없다"를 적기 전에 X 라는 이름의 파일/스테이지를 grep 하고, 그 부재를 변호하는
독스트링이 있는지 읽어라.** 스킬: `the-gap-an-audit-names-may-be-a-docstring-that-defends-it`.

---

## §Ⅴ — 이 세션의 작업 규율 (반복해서 값을 했다)

* **🐘 프로브 PG 의 트리거는 "마이그레이션"이 아니라 "FK 행을 심는 새 테스트"다.**
  #921 에서 이 메모리를 갖고도 트리거를 좁게 기억해 **CI 를 한 번 버렸다**(SQLite 6824 초록 →
  CI 5개 실패). 이후 #922·#923·#924·#925 는 전부 밀기 전에 프로브 PG 로 돌렸고 **#922 에서
  7개, #925 에서 20개**를 미리 잡았다. 프로브는 `pgvector/pgvector:pg16` + `alembic upgrade
  head` 로 **30초**, 전체 스위트 9분. CI 20분을 한 번 버리는 것보다 항상 싸다.
* **🔪 전선 절단은 매번 값을 했고, 두 번은 "절단이 아무것도 안 물었다"가 진짜 구멍이었다.**
  - #921: 워커 배선을 지워도 **알림 테스트 160개가 전부 초록** → 배선 테스트 추가
  - #922: 절단이 **문법 오류**를 내 아무것도 증명 못 함 → 그게 라우트 배선이 미검사임을 드러냄
  - #920: 절단이 **내 테스트 약점**을 잡음(보낸 값 == 저장된 값인데 둘 다 비면 자명하게 참)
* **📐 내가 쓴 산문도 검증 대상이다.** #921 에서 카드 문구를 *"반영했어요"* 로 썼다가
  드레인 전엔 pending 이라 **아직 안 일어난 일을 단언**한다는 걸 절단이 드러냈다.
* **🚧 계약이 세 번 발화했고 세 번 다 예외를 파지 않았다** — R2c(#921, 비동기로 재설계) ·
  common leaf(#923, 조회를 호출자로) · MCP(#914, 같은 파일·같은 패키지 선례라 등록).
* **🩺 배포 검증은 배포된 코드 실행**(정적 grep 아님) · `/app/.venv/bin/python` ·
  공개 URL. **음성 대조군을 항상 같이.** ⚠`docker exec` 에 **`-i` 없으면 heredoc 이 통째로
  침묵**한다 — 빈 출력을 성공으로 읽을 뻔했다.
* **🧭 백그라운드 알림의 "exit 0" 은 체인 마지막 것** — 로그를 열어라.
* **📚 UI 경로는 기억으로 쓰지 마라.** Cloudflare 메뉴 둘을 기억으로 적었다가 형님이
  "그런 행이 없다"고 되물어서야 틀린 걸 알았다. 문서를 먼저 열어라.

---

## §Ⅵ — 다음 세션 시작점

1. **`client_attach` 확인** — BStockReport 가 `client_attach` 인데 **텔레그램 지시가 그 실행
   모드에서 실제로 도는지 아직 안 쟀다.** 세 경로가 닫힌 지금이 자연스러운 순서.
2. **게이트 4 (과금 + 수평확장)** — 가장 크고 제품 결정이 많다. 감사 §Ⅴ. 스키마에
   plan/tier/quota/stripe **0개**. `max_concurrent_runs`(3) + #914 의 토큰 계측이 토대.
   ⚠#914 로 **첫 실측이 나왔다**: 사소한 direct 런 2턴이 **43,187 토큰** → 2M 은 약 46회분.
3. **열린 Medium 보안 5건** — rate limit 부재 · SSE 쿼리토큰 · introspect 무인증
   workspace_id 노출 · device_auth 미검증 client_id · RLS 6/45. 감사 Ⅰ/Ⅱ 에 위치.
   ⚠**착수 전 재측정** — 이 세션에서 감사 주장이 여섯 번 무너졌다.
4. **Receive 스테이지 통합 여지** — #922 는 라우트에서 직접 조회한다. 설계 의도는 파서가
   페이로드에 라우팅 키를 넣고 `receive()` 가 조회하는 것. 충돌하진 않지만 두 경로다.

**SoT**: `docs/audit/multiuser-readiness-2026-09-10.md` (이 세션 결과까지 반영됨)

---

## §Ⅶ — 같은 날 이어진 세션 (§Ⅵ.1 착수) · prod `d35c08e`

### ✅ §Ⅵ.1 답: 텔레그램 지시는 `client_attach` 에서 **실제로 돈다**

런 `08547545`(BStockReport)가 #922 바인딩으로 라우팅 → 형님 머신의
`/Users/blasin/Works/BStockReport-client/wt/08547545` 워크트리 생성 → `verify-slot-0`
컨테이너 기동 → 에이전트 턴 → settle + `worktree prune`. **디스크에 잔재 없음.**

⚠️ 기존 프로브는 이걸 증명하지 **못했다** — 본문이 "무시하세요"라
`knowledge_only` / `delivers_via_local_product_repo: false` 로 분류돼 **라우팅만** 탔다.
*"런이 review_ready 에 도달했다"를 "그 실행 모드가 동작한다"로 읽지 마라.*

### 🚨 그 프로브가 드러낸 결함 → **PR #926 (머지·배포·실행검증 완료)**

검증 게이트 deriver 태스크가 워커에서 **5초 만에 끝났는데**(`task_completed success=True`,
16:33:15→16:33:20) DB 행은 `dispatched` 그대로였고, 백엔드는 180초를 꽉 채운 뒤
`deriver_error: TimeoutError` 를 기록했다. 런은 `review_ready` / `proof_state=untested`.

두 링크가 다 침묵이었다:
1. 워커의 `/workers/result` POST **세 곳 전부 `raise_for_status()` 없음** — `register`(164)와
   poll(646)은 부르는데. non-2xx 를 버리고 바로 `success=True` 를 찍었다.
2. 백엔드 전체에서 태스크 status 쓰기가 **정확히 둘**(482 진입 · 622 정상종료).
   타임아웃 경로는 워커 서브프로세스만 cancel 하고 **행은 영원히 `dispatched`**.
   prod 에 6월 이후 **147건** 누적(138/6/2/1).

`TaskTimeout` 독스트링이 이미 #821·#828 에서 **두 번 오진한 기록**을 갖고 있었다 —
네 칸짜리 표가 **기다리는 쪽 축만** 읽어서 진실이 들어갈 칸이 없었다.
⇒ 스킬 `a-timeout-names-the-waiter-not-the-cause-read-the-other-end`.

**배포 검증(배포된 코드 실행 + 음성 대조군):**
* 워커: 200 → 조용히 반환 **PASS** / 500 → `executor_result_post_rejected` 로그(본문 5000→500자 절단) + raise **PASS**
* 백엔드(prod 컨테이너 `/app/.venv/bin/python`): 진짜 `done` 태스크(출력 262B) flip 시도 → **거절, 출력 보존 PASS** /
  진짜 고아 `cd987b69` → `failed` 전환 **PASS** / 재시도 → **거절 PASS**
* ⚠️ 호스트 워커 3개는 autodeploy **안 됨**(스크립트 전수 확인: `kickstart` 0건).
  `launchctl kickstart -k` 로 재시작함 — 프로세스 시작시각 20:24:06~07 로 확인, 중복 데몬 없음.

### ⚠️ 게이트 4(과금)로 이어지는 귀결
usage 는 `await_completion` 이 **완료 태스크를 반환할 때만** `_from_executor_task` →
`token_budget` 로 누적된다. 타임아웃이면 그 전에 `ExecutorAdapterUnavailable` 로 raise →
**워커가 실제로 태운 토큰이 어디에도 계상되지 않는다.** #926 이 숫자를 되찾아 주지는 않지만
(보고 자체가 안 착지했다) 이벤트를 **시끄럽게** 만들어 그 갭을 측정할 전제를 만든다.

### 📐 §Ⅵ.3 보안 5건 재측정 — **하나는 무너지고 하나는 날카로워졌다**

| # | 주장 | 판정 |
|---|---|---|
| 1 | introspect 무인증 | **확인**(라이브): 200 vs `/api/v1/products` 401 대조군 |
| 2 | device_auth 미검증 `client_id` | **확인 + 정정**(아래) |
| 3 | SSE 쿼리토큰 | **무너짐** — 독스트링 + 스킬이 변호 |
| 4 | RLS 6/45 | **정확히 확인**: 라이브 카탈로그 `6` on / `6` forced / `45`, GUC 미설정 시 fail-open |
| 5 | rate limit 부재 | **확인**: OAuth 표면의 `429` 는 **DCR 하나뿐**, limiter 미들웨어 0 |

* **#3 무너짐**: 브라우저 `EventSource` 는 헤더를 못 보낸다(스킬 `eventsource-sse-auth-trap`).
  `?token=` 은 **같은** `verify_user_jwt` 를 타고 워크스페이스 격리는 구조적이다.
  신뢰 경계가 깨진 게 아니다. 잔여 위험은 **로그에 남는 토큰**뿐 — 훨씬 좁은 주장.
* **#2 정정**: *"무인증"이 결함이 아니다* — 독스트링이 변호하고 RFC 8628 이 요구한다.
  결함은 `client_id` 가 `oauth_clients` 대조 없이 저장되고(scope 는 `ALLOWED_SCOPES` 로 검증한다)
  동의 화면이 그 문자열을 **그대로** 렌더한다는 것:
  `"Allow {clientId} to sign in?"` ([DeviceConsentClient.tsx:158](apps/pwa/app/device/DeviceConsentClient.tsx#L158)).
  **공격자가 형님이 신뢰할 문자열을 고른다.** `oauth_clients.client_name` 이 이미 있으므로
  수정은 좁다: RFC 8628 §3.2 로 미등록 `client_id` 거절 + **등록된 이름**을 렌더.
* 1·2·5 는 따로가 아니라 **한 이야기**다 — 무인증·무제한 토큰 유효성 오라클 옆에
  미검증 device_code 무제한 생성.

### ✅ §Ⅲ.5 (`"enabled": false` 잔해) — **조치 불필요**로 종결
양쪽을 다 읽었다. 응답 모델은 `trigger` 를 **평범한 `dict`** 로 받아 prod 두 행이 그대로 읽힌다.
쓰기는 `TriggerKnob` + `extra="forbid"` 라 `enabled` 를 보내는 클라이언트는 거절되고 새 행은
`{"filters": {}}`. 유일한 잔재는 PG **`server_default`** 인데 리포지터리가 항상 `trigger` 를
넘겨서 휴면 상태다. 마이그레이션 값어치 없음.

### 다음 세션 시작점 (갱신)
1. **device_auth `client_id` 검증 + 동의 화면에 등록된 이름** — 위 #2. 범위가 좁고 준비됨.
2. 게이트 4(과금 + 수평확장) — 위 "귀결" 문단을 입력으로.
3. `oauth` 표면 rate limit(#1·#5 묶어서).
4. §Ⅵ.4 Receive 스테이지 통합 여지 — 미착수.
5. prod 고아 `dispatched` **146건** 잔존(내 프로브 1건은 검증 중 종결). 무해하나 데이터 정리 판단 필요.

### ✅ PR #927 — device consent 가 검증 안 된 이름을 신원으로 렌더하던 것 (prod `c5200cc`)

**⚠️ 내 첫 수정안이 틀렸다 — 코드 쓰기 전에 prod 데이터가 막았다.** "미등록 `client_id` 거절"은
`bsvibe-cli` 가 **의도적으로 미등록**(login.py:513 이 변호: *"public-client flow … security rests on
the human approving a short code — not on client identity"*)이라 **`bsvibe login` 을 통째로 깬다**.
prod 12행 중 10행이 `bsvibe-cli` 였다. ⇒ **거절이 아니라 렌더링이 결함이었다.**

* 쓰기 경로는 **손대지 않음**(diff 로 확인: `start_device_authorization` 변경 0, 라우트는 주석 한 줄).
* `resolve_client_identity` — 보증하는 출처는 둘뿐: 1차 allow-list(`bsvibe-cli`) · **살아있는** DCR 등록.
  **폐기된 등록은 보증하지 않는다**(형님이 revoke 한 게 곧 "더는 보증 안 함").
* 동의 화면: 검증되면 **해석된 라벨**, 아니면 중립 제목 + 원문을 *"Self-reported name — not verified"* 로.
  경고는 판단 근거를 이름에서 **"내가 시작했고 코드가 일치하는가"** 로 옮긴다 — 설계가 말하는 그 근거.
* **⚠️ 내 리뷰 지시도 틀렸다** — "lint-imports 통과하면 OK"를 기준으로 줬는데, 계약 5개 중
  **`backend.identity` 를 source 로 잡는 게 하나도 없어** 그 방향은 빨개질 수 없었다. 그 import 가
  `click` + **321 모듈**을 API 경로에 새로 끌어들이고 있었다(clean main 대조: `click: False`).
  ⇒ 상수를 `backend/shared/oauth_client_ids.py`(guarded common leaf)로 옮기고 **계약 추가**(6 kept).
* **서브에이전트가 한 층 더 갔다**: `lint-imports` 는 **계약 0개여도 exit 0** 이라 새 계약을 지워도
  기존 스모크가 초록이었다 → **계약 이름을 리포트에 못박는** 테스트 추가. 스킬
  `a-check-that-cannot-flip-is-not-measuring-anything` 에 사례로 적재.

**배포 검증(배포된 코드 실행, 대조군 5개 전부 PASS):** `bsvibe-cli`→verified/"BSVibe CLI" ·
live DCR→verified/"Claude Code (bsvibe)" · **`"BSVibe Official Setup"`(피싱 문자열)→unverified/None** ·
`bsvibe-cli-`(근접 오타)→unverified · 미등록 dcr→unverified.
PWA(Vercel, main 머지 시 자동)는 두 로케일 다 서빙 확인.

### ⚠️ 이 세션에 독스트링이 내 오진을 막은 횟수: **5**
run_caps · verify_slots · device_auth 무인증 · `load_run_cap` 의 `None`=uncapped ·
`lookup_public_client` 무인증(*"client_id is already visible in the user-facing URL"*).
⇒ **"X 가 없다/틀렸다"를 적기 전에 그 자리의 독스트링을 먼저 읽어라.** 다섯 번 중 다섯 번 거기 있었다.
