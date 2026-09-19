# BSVibe 세션 인수인계 — 2026-09-19

**워커**: 호스트 2개 — **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인
⚠️ **`com.bsvibe.worker` 는 이제 없다** — #991 로 내렸다. 옛 kickstart 명령을 그대로 쓰면 없는 서비스를 친다.
**열린 작업은 이 문서가 아니라 GitHub 이슈에 있다.** 열린 PR·워크트리는 `gh pr list` / `git worktree list`.

> 🧭 **이 헤더에 전이적인 것을 적지 마라.** 자기 자신을 가리키는 필드는 이 문서를
> 머지하는 순간 거짓이 된다 — 값을 갱신하는 걸로는 못 고치고 **필드를 없애야 고쳐진다.**

> 📦 09-16 · 09-17(2판) · 09-18 은 아직 Notion 아카이브로 안 옮겼다. git 에는 남아 있다.

---

## §0 — 오늘 한 줄: **이 문서가 남긴 "다음에 할 것" 중 하나는 할 일이 아니었다**

착수 항목 넷 중 **①은 다 쟀고, ②는 측정해 보니 존재하지 않는 결함**이었다.
어제 문서가 §Ⅵ·§Ⅷ 에 *"잠복 구멍"* 이라고 적어 남긴 것이 **오진이었고**,
그걸 코드 한 줄이 아니라 **전선 절단**으로 갈랐다.

### 이 세션의 산출물

| | |
|---|---|
| 이슈 **#1000** | codex 두 칸 실측 완료 — 요청 본문 `tools` 배열 **직접 캡처** · `CODEX_HOME` 2×2 |
| 이슈 **#1000** | 본문의 "잠복 구멍" 절 **취소**(측정으로 반증) · 체크박스 2개 닫음 |
| 이 문서 | §Ⅵ·§Ⅷ 의 같은 오진 정정 |

**코드 변경 0.** 오늘은 재는 세션이었다.

## §Ⅰ — 🚨 정정: **`supports_remote_tools` 에 잠복 구멍은 없다**

어제 문서(§Ⅵ·§Ⅷ)와 이슈 #1000 본문이 이렇게 적었다:

> *"게이트가 chat 경로(`adapter.py:491`)에만 있다. agentic 디스패치의
> `_work_tool_surface` 는 `executor_type` 을 안 본다 — 지금도 agentic 작업이
> codex/opencode 로 가면 네이티브를 못 뺏긴 채 돈다."*

**거짓이다. 그 게이트가 agentic 경로다.**

`ExecutorAdapter.chat()` 은 agentic 여부를 **`tools` 에서 파생**한다 —
`agentic=bool(tools)` (`backend/dispatch/adapter.py:553,577`). 거절은 같은 함수의
**같은 `tools`** 로 491 줄에서 **먼저** 터진다. `_work_tool_surface` 는
`_chat_with_session` 안에서만 불리고, 거기 닿으려면 491 을 통과해야 한다.

**전선 절단으로 확인**했다 — `supports_remote_tools` 를 `return True` 로 바꾸니
`tests/dispatch/test_executor_remote_tools.py` 가 **4개 빨개진다**
(`test_agentic_work_on_an_unsupported_executor_is_refused[codex|opencode]` 포함).
게이트는 **2026-07-14 `#553` 이후 쭉** 살아 있었다.

⇒ **왜 두 경로로 읽혔나.** 게이트는 `tools` 라는 이름에, 분기는 `agentic` 이라는
이름에 걸려 있다. `grep supports_remote_tools` 는 **한 줄**을 주고 그 줄이 속한
함수가 어디까지 덮는지는 말해주지 않는다. **한쪽 이름이 다른 쪽에서 파생되는지**를
안 보고 "이름이 다르니 경로도 둘"로 읽었다.

## §Ⅱ — ⭐ #1000 codex: 툴 목록을 **직접 덤프**했다

이슈가 요구한 *"모델이 말하는 것 말고 덤프"* 를 이렇게 만들었다:
**codex 가 모델 제공자에게 보내는 요청 본문의 `tools` 배열을 그대로 캡처.**
로컬 HTTP 서버를 `model_providers.probe.base_url` 로 물리고, 요청을 받아 적고 400 을 돌려준다.
빈 디렉터리 · `--sandbox read-only` · `CODEX_HOME` 격리 · codex 0.130.0.

### 🚨 `--disable unified_exec` 는 실행을 **안 끈다 — 이름만 바꾼다**

| 설정 | 툴 수 | 실행 능력 |
|---|---|---|
| 대조군 | 12 | `exec_command` + `write_stdin` |
| `--disable unified_exec` | 11 | **`shell_command`** ← 모양만 바뀜 |
| `--disable shell_tool` | 10 | **없음** |

둘은 **같은 능력의 두 모양**이다. `unified_exec` 만 끄고 "실행을 뺐다"고 적으면
**정확히 반대**가 된다 — 그리고 모델은 두 경우 다 비슷하게 말한다.

### 전 feature(74개)를 꺼도 남는 5개

`update_plan · request_user_input · apply_patch · web_search · view_image`

* `web_search` → 최상위 `web_search = "disabled"` 로 꺼진다(codex 가 스스로 알려줬다)
* **`apply_patch`(쓰기) · `view_image`(읽기) 는 어떤 플래그로도 안 꺼진다**

### ⭐ 남는 표면이 **모델에 따라 다르다**

| 모델 | 대조군 | 최대 박탈 후 |
|---|---|---|
| gpt-5.5(기본) | 12 | **4** (`apply_patch` 남음) |
| gpt-5.1 · gpt-5.1-codex · o3 | 11 | **3** (`apply_patch` **애초에 없음**) |

⇒ "codex 를 얼마나 벗길 수 있나"는 플래그만으로 안 정해진다. **모델 칸이 필요하다.**

⇒ **판정 🔶 부분 통과**: 실행·에이전트 스폰·웹검색은 전부 끌 수 있다.
그런데 `view_image` 는 모든 조합에서 남는다. 계약이 요구하는 **완전한 0 은 안 된다.**

### 🔎 읽히는데 아무 일도 안 하는 스위치

`tools.view_image=42` 는 *"expected a boolean in `tools.view_image`"* 로 **에러가 난다**
(= 진짜 키다). 그런데 `false` 로 주면 `view_image` 가 **그대로 남는다.**
반대로 `tools.enabled_tools` · `tools.disabled_tools` · `include_apply_patch_tool` 은
틀린 값을 줘도 **에러가 안 난다** — 키 자체가 없다(조용히 무시).

## §Ⅲ — ✅ `CODEX_HOME` 2×2: **인증과 설정은 갈라진다** (#978 과 다르다)

| CODEX_HOME | auth.json | `login status` | 호스트 설정·MCP·memories |
|---|---|---|---|
| 호스트 `~/.codex` | 있음 | Logged in | **전부 붙는다** |
| BSVibe 소유(빈) | 없음 | Not logged in | 안 붙음 |
| **BSVibe 소유 + auth.json 사본** | 사본 | **Logged in** | **안 붙음** ✅ |
| BSVibe 소유 + `OPENAI_API_KEY` | 없음 | Not logged in | (`enable_codex_api_key_env=true` 도 동일) |

**파일시스템까지 격리된다는 증거**: 격리 홈으로 런을 돌리고 그 **세션 id 로 양쪽을 뒤졌다.**
격리 홈의 `sessions/…/rollout-….jsonl` 에서 나오고, 호스트 `~/.codex` 전체에서 **안 나온다.**
⇒ #978 이 claude CLI 에 대해 내린 *"내용 격리는 성립, 파일시스템 도달은 아니다"* 는
**codex 엔 해당 안 된다.**

⚠️ **안 잰 것**: auth.json **사본은 토큰 갱신 때 썩는다.** 심링크로 공유하면 갱신이
**호스트 파일로 되돌아 쓴다** — 그 방향은 안 쟀다.

## §Ⅳ — opencode: 공짜로 얻은 것, 막힌 것

데몬에 **툴 덤프 엔드포인트가 있다**(모델 호출 없이):

```
GET /experimental/tool/ids  → invalid, question, bash, read, glob, grep, edit,
                              write, task, webfetch, todowrite, websearch, skill, apply_patch
```

문서상 이 목록은 **빌트인 + MCP 툴을 함께** 준다 ⇒ **①(MCP 표면 부여)도 공짜로 검증 가능.**

❌ **allowlist 방향은 못 쟀다.** OpenAPI 상 `tools` 는 `map<string, boolean>` 이고
`"*"` 와 개별 이름의 **우선순위가 스키마에 없다.** 실제 턴이 필요하다(= 형님 모델 쿼터).
`Config.tools` · `AgentConfig.tools` 도 같은 모양 — **설정 층**에서 거는 방향도 후보다.

## §Ⅴ — 형님 손에 있는 것

1. **opencode allowlist 실측 승인** — `{"*": false}` + 이름으로 켜기를 확인하려면
   실제 턴 하나가 필요하다. 형님 쿼터를 쓰므로 안 돌렸다
2. Supabase Authentication → Users 의 미확인 테스트 계정 `qazasa123+confirm@gmail.com` 삭제(잔재)
3. **#935 시크릿 로테이션** — #957(KMS key-id)이 선행
4. **#937 잔여** — 재부팅 cold-boot 테스트(sudo)
5. 이월: **#1003** 텔레그램 그룹방 승인 모델 결정 · Notion 아카이브(09-16·09-17·09-18)

## §Ⅵ — 다음 세션 시작점

1. **#1000 opencode 두 칸** — allowlist 방향(①은 `/experimental/tool/ids` 로 공짜) ·
   agentic 런이 `tools` 키를 생략하는 지점(`opencode.py:212` 독스트링이 그대로 적어 뒀다)
2. **#964** settle 스코프 나머지(트랜잭션 분할 선행) · **#954** 미계상 토큰 · **#949** 파리티 잔여 4건
3. codex 를 실제로 올릴 거면 **`view_image` 가 남는 것을 계약이 받아들일지**가 먼저다 —
   플래그를 더 찾는 문제가 아니다(74개 다 껐다)

### 검증 안 된 것 (정직하게)

* **opencode allowlist 전부** — 스키마만 봤다
* **auth.json 심링크 방향**(갱신이 호스트로 되돌아 쓰는지) — 안 쟀다
* codex `apply_patch` 가 gpt-5.5 에만 붙는 이유는 **모델 카탈로그**로 추정일 뿐, 안 팠다
* 어제 문서의 미검증 항목(#1005·#1006 런타임 테스트 4곳 · #970 하루치 · #965 부정 대조군 2건 ·
  `client_attach` verify 게이트 · claim 500 fail-open)은 **그대로 남아 있다**

## §Ⅶ — 이 세션에서 값을 한 규율

* **🔍⭐⭐ `grep` 이 준 한 줄은 그 경로가 어디까지 덮는지 말해주지 않는다.**
  게이트는 `tools` 에, 분기는 `agentic` 에 이름이 걸려 있었고 —
  **`agentic = bool(tools)` 로 파생된다**는 한 줄을 안 보고 "경로가 둘"로 읽었다.
  어제 문서가 그걸 *"작고 명확하다"* 로 넘겨 **다음 세션의 1순위 두 번째 칸**에 올려놨다.
  ⇒ **착수 전 재측정이 오늘 일을 하나 지웠다.**
* **🔪⭐ 없음을 증명한 것도 전선 절단으로 세라.** "게이트가 있다"는 초록으로는 약하다 —
  `return True` 로 바꿔 **4개가 빨개지는 것**을 보고서야 "덮고 있다"가 됐다.
* **📊⭐⭐ 내 하네스가 앞 런의 결과를 이번 런의 답으로 줬다.**
  설정 에러가 나면 codex 는 **요청을 아예 안 보내는데**, 내 프로브는 캡처 파일의
  **마지막 줄**을 읽었다 — 그래서 실패한 런이 직전 런의 12개를 그대로 보고했다.
  **런마다 캡처를 비우고 "NO REQUEST CAPTURED" 를 따로 뱉게** 고친 뒤 재측정했다.
  ⇒ 이건 [[completion-detector-fires-on-an-earlier-success-string]] 와 같은 모양이다.
* **🧪⭐ 혼동 요인을 mtime 으로 판정했으면 오진이었다.** 호스트 `~/.codex` 파일들이
  프로브 중에 계속 움직여 누출로 읽힐 뻔했는데, 범인은 **VS Code ChatGPT 확장이
  18일째 띄워 둔 `codex app-server`** 였다. **세션 id 추적**이 그걸 갈랐다 —
  *"내 행위의 지문"* 으로 재라, 시각으로 재지 마라.
* **🐚 zsh 는 안 감싼 변수를 단어로 쪼개지 않는다.** `for f in $ALL` 이 74개 feature 를
  **한 덩어리 플래그 하나**로 넘겨 *"Unknown feature flag: apps\nbrowser_use\n…"* 가 났고,
  하마터면 *"--disable 은 일부만 받는다"* 로 적을 뻔했다. `${(f)ALL}` 로 고쳤다.
  ⇒ 에러 메시지가 **여러 줄을 한 값으로** 보여주면 그게 신호다.
* **⚙️ 파싱되는 설정 키가 동작하는 키는 아니다.** `tools.view_image=false` 는
  타입 검사를 통과하고(틀린 타입엔 에러를 낸다) **아무 일도 안 한다.**
  ⇒ 설정을 껐다는 근거는 **에러가 안 났다**가 아니라 **표면이 줄었다**여야 한다.
