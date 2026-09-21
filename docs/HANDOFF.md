# BSVibe 세션 인수인계 — 2026-09-21

**워커**: 호스트 2개 — **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인
⚠️ **plist 자체를 고쳤으면 kickstart 로는 안 먹는다** — `launchctl bootout gui/501/<label>` 후
`launchctl bootstrap gui/501 ~/Library/LaunchAgents/<label>.plist`. kickstart 는 plist 를 다시 읽지 않는다.
⚠️ **`com.bsvibe.worker` 는 이제 없다** — #991 로 내렸다. 옛 kickstart 명령을 그대로 쓰면 없는 서비스를 친다.
**열린 작업은 이 문서가 아니라 GitHub 이슈에 있다.** 열린 PR·워크트리는 `gh pr list` / `git worktree list`.

> 🧭 **이 헤더에 전이적인 것을 적지 마라.** 자기 자신을 가리키는 필드는 이 문서를
> 머지하는 순간 거짓이 된다 — 값을 갱신하는 걸로는 못 고치고 **필드를 없애야 고쳐진다.**

> 📦 09-16 · 09-17(2판) · 09-18 · 09-19 는 아직 Notion 아카이브로 안 옮겼다. git 에는 남아 있다.

---

## §0 — 오늘 한 줄: **계획은 맞았는데 계획한 "층"이 토큰을 샜다**

어제 문서의 1순위(#1000 opencode 배선)를 했다. 항목 자체는 실재했고 —
**그 항목이 지정한 구현 층(`(a) MCP 는 설정 층`)이 재보니 쓸 수 없는 것**이었다.
설정 파일로 런 토큰을 주면 **다음 런이 앞 런의 토큰으로 붙는다.** 코드를 쓰기 전에 쟀다.

### 이 세션의 산출물

| | |
|---|---|
| **PR #1014** | opencode agentic 런이 **BSVibe MCP 표면으로만** 행동한다. `_REMOTE_TOOL_EXECUTORS` 에 opencode 추가 |
| PR #1014 | `codex.py` 의 *"NO flag disables it"* 주석 정정 · `INVARIANTS.md` INV-7 #6 정정 |
| 이슈 **#1000** | 체크박스 2개 닫음. 설계가 바뀐 이유(실측표) 코멘트 |
| 스킬 | `a-cached-config-layer-keeps-the-first-credential-it-saw` |
| 체크리스트 | `docs/e2e/opencode-acts-through-bsvibes-tools-checklist.md` |

**모델 토큰 0.** 사전 측정도 라이브 E2E 도 프로바이더 `baseURL` 을 로컬 캡처 서버로 물렸고,
마지막 칸은 **로컬 ollama 모델**로 걸었다(§Ⅴ).

> 📌 **오후에 한 줄이 더 붙었다**: 배선을 끝내고 prod 에서 처음 돌린 opencode 런이 **거짓말을 했다** —
> 잔액 없음이 `review_ready` 로 둔갑했다(§Ⅳ, PR #1019). **배선을 켜기 전엔 없던 노출면이고,
> 켠 첫 런이 바로 밟았다.**

## §Ⅰ — 🚨 설정 파일 경로는 **토큰이 썩는다** (설계가 바뀐 이유)

| 잰 것 | 결과 |
|---|---|
| 데몬이 `?directory=X` 세션에 `X/opencode.json` 을 읽는가 | ✅ 읽는다 — 원격 MCP + 임의 헤더까지 |
| 같은 디렉터리에서 **토큰만 바꾸면** | 🚨 **첫 토큰을 계속 쓴다.** MCP 서버에 요청이 **아예 안 간다** |
| `disconnect` + `connect` 로 갱신되나 | 🚨 **안 된다.** 캐시된 건 연결이 아니라 **설정 스냅샷** |
| `POST /mcp?directory=` 런타임 등록 | ✅ **캐시를 덮는다**. 토큰이 **디스크에 안 남는다**. 디렉터리 스코프 |
| `POST /mcp/{name}/disconnect` | ✅ 표면에서 지워진다 |

워커의 `server_sandbox` 디렉터리는 **모든 태스크·모든 테넌트가 공유하는 한 경로**다(#973).
⇒ 설정 파일이었으면 런 B 가 **런 A 의 run-scoped 토큰**으로 우리 MCP 에 붙는다.
[[worker-auto-memory-leaks-across-tenants]] 와 같은 모양인데 새는 게 노트가 아니라 **쓰기 권한 토큰**이다.

**그래서 셋을 다 한다**: 런타임 등록 · **태스크마다 다른 서버 이름**(동시 태스크가 같은
디렉터리를 쓰므로 고정 이름이면 마지막 등록이 앞을 덮는다) · 런 끝에 disconnect.

### ⭐ 이걸 잡은 센서는 **받는 쪽**이었다

토큰을 바꾼 뒤에도 **툴 목록은 정상으로 떴다** — 캐시가 답해 줬으니까. 보내는 쪽·설정
파일·기능 확인 셋 다 통과한다. 갈라준 건 프로브 MCP 서버의 로그에 **요청이 0건**이었다는 것뿐이다.
⇒ *"동작한다"* 와 *"내 값이 쓰였다"* 는 다른 명제다. 크리덴셜 주입은 **받는 쪽에서** 재라.

## §Ⅱ — 무엇이 들어갔나 (PR #1014 · #1019)

* agentic 런: `POST /mcp?directory=` 로 그 런의 MCP 서버 등록 → `connected` 아니면 **터미널 에러**
  (툴 0개인 에이전트는 없다고 말하지 않고 **지어낸다**)
* 메시지 `tools` 맵: **`{"*": False}` 가 첫 키**, 그 뒤에 우리 이름 — 테스트가 **순서를 단언**한다
* 툴 이름: `<서버키>_<툴이름>` — **원격 MCP 도 stdio 와 같은 규칙**(§Ⅳ 의 "안 잰 것" 하나 닫음).
  우리 건 접두사가 두 번: `bsvibe<taskid>_bsvibe_work_file_read`
* 표면 없는 agentic 태스크는 **거절**(claude_code 와 같은 모양) · 재-spawn 후 **재등록**
* **라이브 E2E**: 진짜 데몬 × 진짜 `OpenCodeExecutor` × 진짜 `build_work_tool_dispatch`
  → 모델에게 간 툴 = **우리 것뿐, 네이티브 0개**
* **PR #1019** — 프로바이더가 **거절한 턴을 성공으로 보고하지 않는다**(§Ⅳ). `info.error` 가 있으면
  터미널 에러. HTTP 상태도(200) 텍스트 유무도(툴로만 행동한 빈 턴은 정상) 신호가 못 된다

## §Ⅲ — ✅ 배포했고, opencode 워커를 **올렸다**

| | |
|---|---|
| prod | **`e7fefff`** (10:19 autodeploy, 컨테이너 재생성 확인) |
| 워커 | 10:28:57 재시작(plist **reload** — kickstart 는 plist 를 다시 안 읽는다) |
| capability | 새 워커 **`4192fdd4`** = `claude_code` + **`opencode`**, 온라인. 옛 행 `2525b5dd` 는 revoke |
| 모델 계정 | **`mac-mini-e2e (opencode)`** = `executor/opencode` 생김 |
| 라이브 데몬 실측 | **prod 워커의 데몬에서 직접** 쟀다 — 이름 규칙 ✅ · allowlist 1개 ✅ · **키 순서 뒤집으면 0개** ✅ |
| 스모크 | 최소 런 `fbe9f905` → **`review_ready`**(84초). 워커 신원을 갈았는데 dispatch 가 멀쩡하다는 실증 — 옛 행을 revoke 해도 **pin 은 fall-through** 한다(`dispatch.py` 의 freshness 게이트) |

### 🚨 그 과정에서 두 번 미끄러졌다 (둘 다 남긴다)

**① 재등록이 엉뚱한 워크스페이스로 갔다.** `bsvibe-worker register` 는 호스트 CLI 세션
(`~/.config/bsvibe/credentials.json`)의 신원을 쓰는데, 그건 **워크스페이스 `6515bfc2`** 다 —
워커가 실제로 일하는 곳은 **`5fa3494c`**(MCP 신원). 새 행이 남의 워크스페이스에 생겼고,
`worker.token` 이 그 토큰으로 **덮였다**. kickstart 하기 **전에** `workers_list` 에 안 보이는 걸
보고 백업에서 되돌렸다 — 그대로 재시작했으면 워커가 `5fa3494c` 를 **통째로 떠났다**.
⇒ 올바른 경로는 **MCP 액세스 토큰**으로 `POST /api/v1/workers/register`.
✅ **잔재는 정리했다**(같은 날): `6eacb96d` `is_active=false` + 그 워커의 모델 계정 2개 삭제 —
`revoke_worker` 가 하는 것과 **같은 동작**을 prod DB 에 직접 넣었다. API 로 못 한 이유가 **#1017**.
⚠️ 그때 `docker exec` 에 **`-i` 가 없어 heredoc 이 psql 에 안 들어갔고**, 출력도 에러도 없이
**아무 일도 안 일어났다** — 사후 SELECT 로 잡았다. *조용한 성공처럼 보이는 건 성공이 아니다.*

**② 내 프로브가 prod 워커를 멈출 뻔했다 — 스토어가 공유다.** 셸의 opencode **1.17.3** 으로
프로브를 돌렸는데, 스토어는 `~/.local/share/opencode/` **하나**고 그게 워커 데몬의 스토어다.
워커(**1.15.12**)가 올라오자 `NOT NULL constraint failed: session_message.seq` —
`opencode.py` 가 이미 적어 둔 바로 그 시그니처다. plist PATH 로 워커를 **1.17.3** 에 맞춰 풀었다
(스토어가 이미 그 스키마고, #1014 측정도 1.17.3 기준). **증상만 없앤 것** ⇒ 이슈 **#1016**.

> 💾 **롤백용 백업**: `~/backups/worker-plists-2026-09-21/` 에 두 plist · `config.json` ·
> `worker.token` · `credentials.json` 의 **변경 전** 사본이 있다. 단 `worker.token` 백업은
> **revoke 된 옛 워커**의 것이라 되돌려도 인증이 안 된다 — 되돌릴 일이 생기면 재등록이 필요하다.

> 🧭 **`launchctl kickstart -k` 는 plist 를 다시 읽지 않는다.** PATH 를 고치고 kickstart 만 하면
> 프로세스는 **옛 PATH** 로 뜬다 — 그래서 1.15.12 가 올라왔다. plist 변경은 `bootout` + `bootstrap`.

⚠️ **부작용 하나**: capability 는 `~/.bsvibe/config.json` **하나로 호스트 전체가 공유**한다.
그래서 admin 워커도 opencode 를 wire 하려 들고, 자기 몫의 serve 를 못 띄워 **매 부팅
`opencode_serve_disabled` 에러를 찍는다**(#970 의 로그 노이즈에 한 줄 추가된 셈).
capability 가 **워커별이 아니라 호스트별**이라는 게 근본 원인이다.

## §Ⅳ — 🚨 첫 opencode 런이 거짓말을 했다 (PR #1019)

배선이 끝나고 **prod 에서 처음 돌린 opencode 런**에서 나왔다. 형님 계정에 잔액이 없었고,
opencode 는 그걸 이렇게 돌려준다:

```
HTTP 200
{"parts": [], "info": {"error": {"name": "APIError",
  "data": {"message": "Upstream request failed: Insufficient account funds", "statusCode": 402}}}}
```

**200 이고 텍스트 파트가 없다.** 텍스트만 읽던 실행기는 **빈 성공**으로 읽었다:

| | 고치기 전 (`b0429ba3`) | 고친 뒤 (`dde232cf`) |
|---|---|---|
| 태스크 | `done` × 3, 출력 0 | **`failed`** × 3 |
| 사유 | 없음 | `opencode turn failed — APIError (HTTP 402): Insufficient account funds` |
| 런 | **`review_ready`** (거짓) | **`failed`** |
| 부작용 | **딜리버러블 발행됨**(discard 시 retract) | 없음 |

같은 계정·같은 프롬프트·같은 워커. 바뀐 건 #1019 뿐이다.

## §Ⅴ — ✅ #1000 체크리스트가 닫혔다 (로컬 모델, 토큰 0)

형님 제안대로 ollama `qwen3-coder:30b` 를 워커 샌드박스 디렉터리에 물려 마지막 칸을 걸었다.
증거는 서술이 아니라 **opencode 세션의 tool 파트**다:

```
TOOL bsvibee96ff6d58242_bsvibe_work_file_read | status: completed
  output: {"result": "# bsvibe-app\n\nBSVibe AI agent OS — unified monorepo …"}
```

모델이 opencode 네이티브 `read` 가 아니라 **우리 런 스코프 이름**으로 불렀고, opencode 가 그걸
**우리 서버에 실행**했고, prod `README.md` 가 돌아왔다.

* 설정: `~/.bsvibe/sandbox-cwd/opencode.json` (워커 전용 — 형님 터미널 opencode 는 안 건드린다).
  **데몬이 디렉터리 설정을 캐시**하므로 바꾸면 워커 재시작
* ⚠️ **툴 실행은 정확했는데 최종 답변이 툴콜 JSON 을 에코**했다 — 배선이 아니라 **모델 품질**
* ⚠️ **속도**: 콜드 3분 48초/턴, 웜 4~5분/턴 ⇒ **검증용**이지 상시용이 아니다
* 재현 레시피는 메모리 `opencode-serve-mcp-runtime-registration` 에 있다

## §Ⅵ — 형님만 할 수 있는 것 (결제 · 콘솔 · 제품 결정)

> 🧭 **이 절이 짧아야 정상이다.** 이 세션에서 내가 여기 올렸던 항목 넷 중 **둘은 내가 할 수 있는
> 일이었다**(잔재 정리 · opencode 런). 형님이 *"남은 것도 결국 해야 하잖아"* 라고 한 게 맞다 —
> **막힌 것과 내가 안 한 것을 섞지 마라.** 아래는 진짜로 형님 계정·형님 손이 필요한 것만이다.

1. **opencode 계정 잔액** — 로컬 모델로 검증은 끝났지만 실제 작업엔 못 쓴다(턴당 4~5분).
   충전하면 워커 설정에서 `model` 줄만 지우고 재시작하면 원래 모델로 돌아간다
2. **Supabase Authentication → Users** 의 미확인 테스트 계정 `qazasa123+confirm@gmail.com` 삭제
3. **#937 잔여** — 재부팅 cold-boot 테스트(sudo)
4. **#1003** 텔레그램 그룹방 승인 모델 **제품 결정** · **codex 계약 결정**(§Ⅶ.3)
5. **#935 시크릿 로테이션**의 최종 실행 — 코드(#957)는 에이전트 작업이고, 키 교체는 형님 손

## §Ⅶ — 다음 세션이 할 것 (전부 에이전트 작업이다)

> 아래는 **막혀 있지 않다.** 코드·측정·PR 로 진행할 수 있고, 형님 결정이 필요한 지점은
> 각 항목 안에 따로 적어 뒀다.

1. **🚨 #1017 — CLI 가 prod 에서 죽어 있다.** `bsvibe products list` 가 401.
   발급자는 BSVibe-Auth(`iss=api.bsvibe.dev`, kid 는 `api.bsvibe.dev/.well-known/jwks.json` 에 **있다**)인데
   API 의 `USER_JWT_JWKS_URL` 은 **Supabase** 를 가리킨다. PWA(Supabase JWT)는 멀쩡해서 안 보였다.
   **첫 걸음**: 검증기가 **둘**이라는 것부터 확인해라 — `shared/authz/auth.verify_user_jwt`(Supabase 만)와
   `api/v1/workers_register_auth`(BSVibe-Auth 도 받는다). 같은 토큰이 한쪽은 통과·한쪽은 401 이다.
   **회귀 가드가 핵심**: 지금 스위트는 전부 초록인데 prod CLI 는 죽어 있다 — 테스트가 **자기가 서명한
   토큰**을 쓰기 때문이다. prod env 변경은 형님 확인이 필요하지만 **코드·테스트는 지금 할 수 있다**
2. **#1016 — 워커 opencode 스토어를 BSVibe 소유로.** 지금은 형님 터미널 opencode 와 **같은 파일**
   (`~/.local/share/opencode/`)이라, 형님이 opencode 를 **띄우기만 해도** 워커가 멈출 수 있다
   (오늘 내가 그렇게 멈췄다). 손잡이는 이미 있다 — `WorkerSettings.opencode_data_dir`, **기본값이 공유**일 뿐이다.
   **양성 대조군**: 가른 뒤 호스트 셸에서 opencode 를 띄워 놓고 워커 런이 멀쩡한지 본다
3. **codex** — `view_image` 가 남는 것을 계약이 받아들일지가 **제품 결정**(§Ⅵ.4)이고,
   받아들이면 배선은 #1014 와 같은 모양이다. 단 codex 는 헤더가 아니라 **env var** 로 베어러를 받으므로
   어댑터에 codex 전용 모양이 필요하다. **플래그를 더 찾는 문제가 아니다**(74개 다 껐다)
4. **#964** settle 스코프 나머지(트랜잭션 분할 선행) · **#954** 미계상 토큰 · **#949** 파리티 잔여 4건
5. 잡일: **Notion 아카이브**(09-16~09-21) — Notion MCP 가 붙어 있으니 에이전트가 할 수 있다 ·
   **#970** 워커 로그 노이즈(admin 워커가 매 부팅 `opencode_serve_disabled` 를 찍는다, §Ⅲ 참고)

### 검증 안 된 것 (정직하게)

* **부분 표면은 감지 못 한다.** `connected` 는 서버 단위 신호다. opencode 1.17.3 엔 claude 의
  init 이벤트 대응물이 **없다** — `/experimental/tool{,/ids}` 는 **네이티브만** 돌려주고 MCP 가
  붙은 디렉터리와 안 붙은 디렉터리의 답이 **같다**(실측). 우리 서버가 붙었는데 툴을 **덜**
  광고하면 그대로 돈다. (사고를 만든 **레이스**는 못 일어난다 — 세션 생성 **전에** 동기 확인)
* 실제 BSVibe 엔드포인트에 **붙는 것**까지는 봤다(§Ⅴ). 다만 **광고된 툴이 9개 전부인지 개수로는
  안 셌다** — 모델이 그중 둘을 불렀을 뿐이다
* **워커가 강제 종료되면** 등록이 안 내려가고 다 쓴 토큰을 든 연결이 데몬에 남는다(재시작까지)
* opencode 가 MCP 툴 호출에 **실패**했을 때의 표면(권한·에러 모양)은 안 봤다
* auth.json 심링크 방향 · codex `apply_patch` 가 gpt-5.5 에만 붙는 이유 — 09-19 에서 그대로 이월
* 09-18 문서의 미검증 항목(#1005·#1006 런타임 테스트 4곳 · #970 하루치 · #965 부정 대조군 2건 ·
  `client_attach` verify 게이트 · claim 500 fail-open)도 **그대로 남아 있다**

## §Ⅷ — 이 세션에서 값을 한 규율

* **🔐⭐⭐ 캐시된 설정 층은 처음 본 크리덴셜을 계속 쓴다.** 그리고 **기능은 멀쩡히 돌아간다** —
  캐시가 답하니까. 보내는 쪽에서는 절대 안 보인다. **받는 쪽을 계측해라**: 토큰을 바꾼 뒤
  서버에 요청이 **새로 오는가**. 스킬 `a-cached-config-layer-keeps-the-first-credential-it-saw`
* **📐⭐ 인수인계가 지정한 "층"도 주장이다.** 어제 문서는 항목만 맞힌 게 아니라 **구현 층까지**
  적어 놨고(`MCP 는 설정 층`), 그 층은 **읽히기는 한다** — 첫 번째 읽기에 대해서만 참이었다.
  ⇒ *"X 로 하면 된다"* 를 받으면 **X 가 두 번째 런에서도 참인지**를 재라
* **🪞⭐ 재연결이 재-읽기는 아니다.** `disconnect`+`connect` 면 새 설정을 집어올 거라는 건 추측이다.
  실측하니 스냅샷은 그대로였고, 캐시 무효화 경로는 문서에 없고 **덮어쓰는 API 만** 있었다
* **🔪 이번에도 전선 절단이 갈랐다.** 재-spawn 후 재등록 한 줄을 지우니 빨개진다 — 초록만으로는
  "재등록한다"가 증명 안 된다
* **📡 토큰 0 측정이 이제 기본 도구다.** 09-19 의 캡처 서버 수법이 이번엔 **사전 설계 검증**에
  그대로 재사용됐다(하네스가 스크래치패드에 남아 있었다). 프로바이더 `baseURL` 만 돌리면 된다
* **🔑⭐⭐ 베어러 검증기가 둘인데 신뢰 루트가 다르면, 한쪽 성공이 다른 쪽을 보증하지 않는다.**
  같은 토큰으로 `workers/register` 는 **성공**하고 `DELETE /workers/{id}` 는 **401** 이었다.
  처음엔 만료로 오진했는데(refresh 하니 register 가 통과해서 더 그럴듯했다), 진짜 이유는
  **JWKS 신뢰 루트**였다. ⇒ 401 을 만나면 **에러 본문을 끝까지 읽어라** — "invalid bearer" 와
  "JWKS resolution failed" 는 다른 병이다(**#1017**)
* **🎭⭐⭐ 능력을 켠 첫 런이 그 능력의 노출면을 밟는다.** #1014 는 opencode agentic 경로를 **열었고**,
  그 경로로 처음 돈 런이 *"프로바이더 에러가 200 안에 숨는다"* 를 즉시 드러냈다(§Ⅳ).
  그 결함은 코드로는 전부터 있었지만 **닿을 수 없어서 안 보였다**. ⇒ 막혀 있던 경로를 열면
  **그 경로를 한 번 걸어라** — 리뷰로는 안 나온다
* **🧾⭐⭐ "형님 손"과 "내가 안 한 것"을 섞지 마라.** 이 세션에서 §Ⅳ 에 올린 넷 중 **둘은
  내가 할 수 있는 일**이었다(잔재 정리·opencode 런). 형님이 *"남은 것도 결국 해야 하잖아"* 라고
  되물어서야 갈렸다. ⇒ 인계 목록을 쓸 때 항목마다 **"내가 못 하는 이유"를 한 줄로 적어라** —
  못 쓰면 그건 내 일이다
* **🏢⭐⭐ 같은 호스트의 두 CLI 신원이 다른 워크스페이스를 가리킨다.** `bsvibe-worker register` 는
  호스트 로그인 신원을 쓰고, MCP 는 다른 신원이다. **`register` 는 upsert 가 아니라 항상 새 행**이라
  틀린 워크스페이스에 만들고 **로컬 토큰까지 덮는다**. kickstart 전에 `workers_list` 로 확인한 게
  워커를 통째로 잃는 걸 막았다. ⇒ **신원을 쓰는 명령은 "어느 워크스페이스로 갔나"를 즉시 되재라**
* **💾⭐⭐ 내 측정 도구가 피험자의 상태를 마이그레이트했다.** 프로브를 스크래치 디렉터리에서 돌려도
  **SQLite 스토어는 공유**였다. 신버전이 스키마를 올리자 구버전 prod 데몬이 죽었다 — 그리고 그
  실패 모드는 우리 코드가 **이미 주석으로 적어 둔 것**이었다. ⇒ **읽기 전용 프로브라도 "이게 쓰는
  상태가 누구 것인지"를 먼저 물어라**(#1016)
* **🧭 능력을 켜기 전에 "그걸 돌릴 놈이 있나"를 세라.** 배선을 다 끝내고서야 **온라인 워커에
  opencode capability 가 없다**는 걸 봤다. 코드 리뷰로는 안 나온다 — `workers_list` 한 번이다
