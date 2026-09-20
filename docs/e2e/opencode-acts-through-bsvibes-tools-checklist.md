# E2E — opencode 가 BSVibe 의 툴로만 행동한다 (#1000)

**대상 PR**: opencode agentic 런에 MCP 표면을 주고 네이티브 툴 11개를 박탈
**전제**: 호스트 워커는 **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` 후
**프로세스 시작 시각**으로 확인.

> 🧪 **모델 토큰 0으로 걸 수 있다.** 확인하려는 것은 모델의 대답이 아니라
> **모델에게 무엇이 갔는가**이고, 그건 요청 본문에 있다. 프로바이더 `baseURL` 을
> 로컬 캡처 서버로 물리면 `tools` 배열을 그대로 받아 적는다 — 쿼터를 안 쓴다.
> (2026-09-19 규율: *"모델에게 물어보지 마라"* 의 다음 칸은 *"모델을 부르지도 마라"*.)

---

> ✅ **2026-09-21, 머지 전에 로컬 실측으로 걸었다.** opencode 1.17.3 · 실제
> `opencode serve` 데몬 · 실제 `OpenCodeExecutor` · 실제 `build_work_tool_dispatch`.
> 아직 **prod 배포 후**로 남은 칸은 §배포 후 에 있다.

## 사전 측정 — 설계가 성립하는지 (전부 실측)

- [x] 긴 수명 데몬이 `?directory=X` 로 만든 세션에 **X/opencode.json 을 읽는가** → **읽는다**
- [x] 원격(HTTP) MCP + 임의 헤더를 받는가 → **받는다** (`Authorization: Bearer …` 도달 확인)
- [x] 원격 MCP 툴의 이름 규칙이 stdio 와 같은가 → **같다**: `<서버키>_<툴이름>`
      (우리 표면은 `bsvibe<taskid>_bsvibe_work_file_read` — 접두사가 두 번 붙는다)
- [x] `{"*": false, <우리 이름>: true}` 가 네이티브를 전멸시키는가 → **전멸**(11 → 우리 것만)
- [x] 🚨 **키 순서** — 뒤집으면 → **0개, 에러 없음**. 원격 MCP 이름으로도 재현
- [x] 🚨 **설정 파일 경로는 토큰이 썩는다** — 같은 디렉터리에서 토큰을 바꿔도 데몬이
      **첫 토큰을 계속 쓴다**. `disconnect`+`connect` 로도 안 갱신된다
- [x] `POST /mcp?directory=` 런타임 등록은 **캐시를 덮고**, 토큰이 **디스크에 안 남고**,
      **디렉터리 스코프**다(다른 디렉터리 세션엔 안 보인다)
- [x] `POST /mcp/{name}/disconnect` 가 표면에서 **지운다**(다시 11개)

## 유닛 — 초록이 무엇을 증명하는가

- [x] `tests/executors/worker/test_opencode_remote_tools.py` 10개 통과
- [x] **전선 절단**으로 감지력 확인: 재-spawn 후 재등록 한 줄을 지우면 **빨개진다**
- [x] `ruff check` · `ruff format --check` 통과

## 라이브 — 실제 데몬 × 실제 executor (토큰 0)

- [x] `build_work_tool_dispatch` 의 **진짜 산출물**로 `OpenCodeExecutor().execute()` 를 돌린다
- [x] MCP 서버가 받은 헤더 = 그 런의 토큰 (`Bearer LIVE-E2E-RUN-TOKEN`)
- [x] MCP 서버가 받은 메서드 = `initialize` · `notifications/initialized` · `tools/list`
- [x] **모델에게 간 툴 = 우리 것뿐** — `bsvibeaa11bb22cc33_bsvibe_work_file_read` ·
      `…_bsvibe_work_shell_exec`. opencode 네이티브 **0개** ✅
- [x] 런 종료 후 등록이 사라진다(`disconnect`)

## 배포 후 — prod 에서 확인할 것 (아직 안 함)

- [ ] 워커 kickstart → **프로세스 시작 시각**으로 새 코드 확인
- [ ] opencode 계정으로 **agentic 런 1회** — `review_ready` 까지 완주
- [ ] 워커 로그에 `opencode_run_mcp_registered` 가 그 런의 task_id 로 찍힌다
- [ ] 백엔드 MCP 액세스 로그에 그 런의 토큰으로 `bsvibe_work_*` 호출이 찍힌다
      (= 표면이 장식이 아니라 **실제로 쓰인다**)
- [ ] **음성 대조군**: 같은 워커의 다음 런이 **다른 서버 이름**으로 등록된다
      (공유 디렉터리에서 이름이 겹치면 앞 런의 토큰을 물려받는다 — 이게 이 설계의 이유)
- [ ] 런 종료 후 `GET /mcp?directory=<sandbox>` 에 그 이름이 `disabled` 로 남는다

## 안 잰 것 (정직하게)

* **부분 표면은 감지 못 한다.** `connected` 는 서버 단위 신호다. opencode 1.17.3 엔
  claude 의 init 이벤트에 해당하는 게 없다 — `/experimental/tool{,/ids}` 는 **네이티브만**
  돌려주고 MCP 서버가 붙은 디렉터리와 안 붙은 디렉터리의 답이 **같다**(실측).
  ⇒ 우리 MCP 서버가 붙었는데 **툴을 덜 광고하면** 그대로 돈다.
* **실제 BSVibe MCP 엔드포인트로는 안 걸었다** — 라이브 E2E 의 MCP 서버는 툴 2개짜리
  프로브다. 붙는 것·헤더·이름 규칙·박탈은 증명됐지만, **9개가 다 뜨는지는 prod 에서** 본다.
* opencode 가 **MCP 툴 호출에 실패했을 때**의 표면(권한/에러 모양)은 안 봤다.
