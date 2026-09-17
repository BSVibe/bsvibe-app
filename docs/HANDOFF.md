# BSVibe 세션 인수인계 — 2026-09-17

**prod 코드**: `d9b05e0` (#994 — CLI config dir 이전. 워커 재시작 필요했음)
**워커**: 호스트 3개 — **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인
**열린 작업은 이 문서가 아니라 GitHub 이슈에 있다.** 열린 PR·워크트리는 `gh pr list` / `git worktree list`.

> 🧭 **이 헤더에 전이적인 것을 적지 마라.** 자기 자신을 가리키는 필드는 이 문서를
> 머지하는 순간 거짓이 된다 — 값을 갱신하는 걸로는 못 고치고 **필드를 없애야 고쳐진다.**

> 📦 **직전 판(09-16, `55c0eab`)은 아직 Notion 아카이브로 안 옮겼다.** git 에는 남아 있다.

---

## §0 — 오늘 두 세션이 있었다. 앞 세션의 결론 하나는 **스스로 반증**됐다

### 오전: 워커 Claude OAuth 재로그인 (#987·#988·#989)

`bsvibe-worker claude-login` 이 세 번 연속으로 고쳐졌다 — 400 의 이유를 노출 →
플랫폼 **OOB redirect**(loopback 은 더 이상 안 받는다) → authorize `state` 에
**32바이트 엔트로피**(22자는 거부된다). `~/.bsvibe/claude_oauth.json` 재발급 완료.

⇒ **§Ⅲ 의 "워커 OAuth 재로그인"이 닫혔다.** 19일간 5분마다 돌던
`claude_oauth_refresh_invalid_grant` 트레이스백(로그의 96.7%)이 멈췄다.
**#970 은 그래도 닫지 않는다** — 다음 만료 때 같은 폭주가 그대로 재발한다.

### ⛔ 그 세션이 스스로 정정한 것 — 인증은 프레이밍 행의 원인이 **아니었다**

런 히스토리가 반증한다: `3a72b58d` 가 **09-16 05:57 에 이미 완주**했고, 인증 수정은
**22시간 뒤**다. 프레이밍 행의 원인은 09-16 의 autofs 경로 그대로다(#965 세 번째 코멘트).

> *"내 수정 후 정상"은 **수정 전에도 정상이었는지 확인하기 전까지 아무 의미가 없다.**"*
> 이 이슈에서만 **세 번째** 오귀인이었다(redirect_uri → 복사 손상 → 인증).
> 앞의 둘은 대조 실험으로 잡았고, 이번엔 **이미 존재하던 기록(런 히스토리)을 먼저 안 봤다.**

## §Ⅰ — 오후: #965 재전달 구현 (PR #990, prod 배포 + 검증 완료)

### 설계 보고서의 모양이 틀렸고, 고친 쪽이 더 작았다

오전 세션의 dogfood 조사가 옵션 A(claim)를 추천했고 그대로 갔지만, **재전달 주체는
스위퍼가 될 수 없다.** 페이로드의 두 조각이 *일부러* 행에 저장되지 않는다:

* `mcp` — `issue_run_task_token` 이 **디스패치 시점에 민팅**하는 run-스코프 토큰
* `env` — exec 태스크의 시크릿. 주석이 명시한다: 명령 문자열에 넣으면
  *"a value written to the database and kept in Redis for good"*

⇒ 스위퍼는 **시크릿을 영속화하지 않고서는 재구성할 수 없다.** 신뢰성 수정을 보안
회귀로 결제하는 셈이다. 디스패치 호출자 **둘 다** 바로 다음 줄에서 `await_completion`
을 부르므로 **awaiter 가 방금 보낸 것을 그대로 들고 있다.**

덤으로 **F10(남은 마감)이 공짜로 풀리고**, awaiter 가 죽으면 재전달이 없다 — 이건
결함이 아니라 옳은 동작이다(기다리는 사람이 없는 작업을 되살리면 슬롯을 태워 허공에 보고한다).

### 보고서의 F 항목 — 8개 확인, 1개 정정

전부 재측정했다. **F6 은 정정**: 보고서가 *"다른 워커로 재배정하면 늦은 결과가 거부된다"*
를 공짜 안전장치로 적었는데, **같은 워커로 재전달하면 `worker_id` 검사는 통과한다.**
막는 건 `status='dispatched'` 쪽이고, 그게 **같은 워커 재전달이 안전한** 근거다.

### 들어간 것

| | |
|---|---|
| `executor_tasks.claimed_at` | 원자적 조건부 UPDATE. 중복 방어가 **타이머가 아니라 WHERE** 에서 나오므로 1시간 턴도 구조적으로 안전 |
| `POST /api/v1/workers/claim` | `record_result` 의 H1 바인딩 상속. 모든 거절은 동일한 `claimed: false` |
| 워커 | 실행 전 claim. 거절=안 돌림(fail-closed) · transport 에러=그냥 돌림(fail-open) + **로컬 dedupe** 가 그 fail-open 을 받친다 |
| `executor_workers.protocol_version` | `X-BSVibe-Worker-Protocol` 헤더로 **매 요청**. 기본 1 = 오늘 동작 그대로 |

**기존 `run_once` 는 execute 중복을 전혀 안 걸렀다** — `_RUNNING_TASKS` 는 cancel 분기
전용이었다. 테스트로 실증했다(`executed twice`).

⚠️ **바디가 아니라 헤더인 이유**: `HeartbeatBody` 가 `extra="forbid"` 라 새 바디 필드는
**옛 백엔드가 모든 하트비트를 422 로 거절**해 워커를 통째로 오프라인시킨다.
`capabilities` 도 안 되는데 **등록 때 한 번만** 가서 지금 도는 빌드를 말해주지 않는다.

### prod 검증 — 실제로 유실을 복구했다

```
07:07:03.238  executor_task_dispatched    710b4b52 → 2525b5dd
07:07:33.496  executor_task_redelivered   710b4b52  remaining_s=269.7   ← 유예 정확히 30s
07:07:33.500  executor_task_redispatched  710b4b52  timeout_s=269.74    ← 원래 300s 아님
16:08:46      task_duplicate_dropped + task_claimed (같은 task_id)      ← 중복 걸러짐
             런 593dd849 → review_ready (claim 까지 102.83s)
```

**이 변경 전이라면 300s 타임아웃으로 실패했을 런이다.** 정상 런(`b737f9a6`)은
영수증이 0.17~4.59s 에 찍히고 재전달 0건.

## §Ⅰ.5 — 같은 세션에 #970·#978 도 닫았다 (PR #993 · #994, 배포·검증 완료)

### #970 로그 소음 — 재다가 **이슈에 없던 원인**이 나왔다

실측: 워커 로그 하나가 **1,817,107줄 · 264MB**, 그중 **96.4%가 rich 트레이스백**.
`claude_oauth_refresh_failed` 6,164회 × 약 284줄이고 **6,163회(99.98%)가 invalid_grant**.
코드는 이미 확정이라 분류해 놓고 그 직후에 284줄을 덤프하고 있었다.

🔍 **워커는 `configure_logging` 을 한 번도 안 불렀다.** structlog 의 *설정되지 않은*
기본값 = 개발용 ConsoleRenderer + rich 예외로 prod 에서 돌고 있었다. 예외 하나가
**14줄·1909B → 1줄·277B**, ANSI 제거. 볼륨보다 큰 값은 **`jq` 로 읽힌다**는 것 —
ANSI 콘솔 덤프가 이번 조사를 실제로 지연시켰다.

⇒ 배포 후 실측: 재기동 이후 **11줄 전부 JSON · ANSI 0 · 트레이스백 0**.
`BSVIBE_WORKER_LOG_LEVEL` 양성 대조군 통과(`info` 숨김 / `debug` 보임).

### #978 하네스 — **09-16 의 "못 닫는다"가 뒤집혔다**

`CLAUDE_CONFIG_DIR` 은 09-16 에 `Not logged in` 으로 기각됐는데, **그날 워커 OAuth 가
만료돼 CLI 가 호스트 크리덴셜 파일로 인증하고 있었고 그 파일이 하필 옮기려는
디렉터리 안에 살기** 때문이었다. 2×2 를 네 칸 다 실제 CLI 로 돌려 확정했다:

| | 호스트 config dir | BSVibe config dir |
|---|---|---|
| 토큰 주입됨 | 인증됨 | **인증됨** |
| 토큰 없음 | 인증됨(호스트) | `Not logged in` |

오전의 재로그인이 우리를 **위쪽 행**으로 옮겨 줬다. 실측(`--debug-file`):
`~/.claude/` **10 → 0**, `~/.claude/plugins` **2 → 0**, BSVibe dir **0 → 39**.

**생명줄은 살아남는다** — 크리덴셜 두 경로 모두 우리 Python 이 `Path.home()` 으로
직접 읽어 env 로 주입한다. 워커 크리덴셜을 일부러 태워 실측했다.

### ⭐⭐ #965 를 일부러 재현했고, 안 멈췄다

호스트 `known_marketplaces.json` 의 `installLocation` 을
`/home/vscode/...` 로 **주입**하고 런을 쐈다 — 09-16 에 prod 전체를 300초 타임아웃으로
멈춘 것과 **똑같은 상태**. 결과: **40초 만에 `review_ready` 완주.**
원복 후 백업과 바이트 단위 동일 확인.
⚠️ 원래 값: `"/Users/blasin/.claude/plugins/marketplaces/claude-plugins-official"`

### 남는 것

* `/Library/Application Support/ClaudeCode` 9건은 **안 닫힌다.** 이 호스트에 **그 경로가
  없고**(프로브만 한다) 쓰려면 **root 가 필요하다** — `~/.claude` 와 위험 등급이 다르다
* **로그 로테이션은 #970 에 남아 있다.** launchd 가 fd 를 쥐고 있어 `newsyslog` 가
  파일을 옮겨도 워커는 옛 inode 에 쓴다 — 재시작이 있어야 새 파일을 잡는다
  (배포마다 하는 kickstart 가 마침 그 역할). sudo 필요 = 형님 손
* 기존 로그 776MB 는 **그대로다.** 이 PR 들은 증가분만 줄인다

## §Ⅱ — 🚨 검증 중 드러난 것: 두 데몬이 **하나의 워커 identity** (#991)

**#965 가 만든 문제가 아니다 — 3개월 된 상태다.**

`com.bsvibe.worker`(이름 `mac-mini-executor`)와 `com.bsvibe.worker-mac-mini-e2e` 는
**둘 다 토큰 지정 없이 떠서 같은 `~/.bsvibe/worker.token` 을 읽는다.**
`BSVIBE_WORKER_NAME` 은 *등록 시점*에만 쓰이고 `worker_token_loaded source=file` 이 이긴다.

* `executor_workers` **8행 중 하루 안에 하트비트한 건 2행뿐**
* `mac-mini-executor` 행(`463f766e`) 마지막 하트비트 **2026-06-11**
* **결정적 실험**: pid 1054 를 `SIGSTOP` 했는데 런이 그대로 완주했다. **둘 다** 얼려야 멈췄다

⇒ 용량 회계(`last_in_flight`)가 틀리고, 실효 병렬도가 `max_parallel_tasks` 의 **2배**다.
그리고 **"워커를 껐는데 왜 계속 돌지"가 재현된다** — 이번 세션에서 실제로 한 번 밟았다.

**#965 와의 관계**: 로컬 dedupe 는 **프로세스별**이라 재전달본이 다른 데몬에 떨어지면
로컬로는 못 거른다. **백엔드 claim 이 잡으므로 정확성은 유지된다** — 1층이 claim,
2층이 로컬이라는 순서가 마침 이 상황에서 맞았다.

## §Ⅲ — 형님 손에 있는 것

1. **#991 워커 identity 분리** — 데몬마다 `BSVIBE_HOME` 을 가르거나
   `BSVIBE_WORKER_TOKEN` 을 박는다(admin 이 이미 그 모양)
2. **#965 부정 대조군 2건** — prod DB 에 손으로 UPDATE 를 쳐야 해서 못 걸었다.
   ⚠️ `protocol_version=1` 을 걸고 복구를 잊으면 **재전달이 영원히 안 걸리고 에러도 안 난다**
   (워커가 다음 하트비트에 2 로 되돌리므로 자가복구되긴 한다)
3. 이월: **#935 시크릿 로테이션 2건** · **#937 리포 밖** · 09-16 판 Notion 아카이브

---

## §Ⅳ — 다음 세션 시작점

1. **🔴 codex·opencode 가 원칙을 위반한다 — 형님 결정 대기** — `_REMOTE_TOOL_EXECUTORS`
   가 `claude_code` 하나다. 선택은 **MCP 표면 부여 + 네이티브 박탈** 또는 **agentic 제외**
2. **#991 워커 identity** — 위 §Ⅱ. 고치는 곳이 launchd plist 라 형님 손
3. **#970 잔여 = 로그 로테이션 하나** — 나머지 세 항목은 #993 으로 닫혔다
4. **#957 KMS key-id** · **#964 settle 스코프** · **#928 과금**(**#953 이전 숫자로 튜닝 금지**)

### 검증 안 된 것 (정직하게)

* **#970 의 "하루치 증가분" 비교를 못 했다** — 재기동 후 런 한 사이클 증가분이 세 워커
  합 **2,488B**(이전엔 `claude_oauth_refresh_failed` 한 건이 1,909B)지만 창이 짧다
* **#970 의 확정-실패 시나리오를 prod 에서 안 걸었다** — 워커 크리덴셜을 일부러
  태워야 보이고 그건 prod 인증을 만지는 일이다. 단위는 전선 절단 7건으로 덮였고,
  폴백 경로 자체는 #978 검증 중 실측했다

### ⭐ 다음 **긴 런 하나**로 세 개를 한꺼번에 봐라

검증 런이 전부 수 초짜리여서 셋 다 못 봤다. 세 개가 **같은 조건**(수 분 이상 도는
실제 `act` 턴)을 요구하므로 따로 걸 이유가 없다:

1. **긴 턴이 재전달되지 않는다** — `executor_task_redelivered` 0건.
   **이 설계가 lease 대신 claim 을 고른 유일한 이유**라 prod 에서 한 번은 봐야 한다
2. **#966 체크리스트 3번** — 긴 `act` 턴이 안 잘리는지(`agent_loop.act` 는 3600s).
   프레이밍이 죽어 있던 동안 못 봤고 지금은 볼 수 있다
3. **#970 하루치 로그 증가분** — 그 런을 포함한 하루를 배포 전 하루와 비교

⇒ 시작할 때 `claimed_at` 과 재전달 로그(**`bsvibe-prod-worker-1` 컨테이너**, backend 아님)를
같이 걸어 두고, 끝나면 세 칸을 한 번에 채워라.
* `client_attach` verify 게이트 회귀 · claim 500 fail-open · `cancel` 경로 —
  전부 단위로만 덮였다. 이유는 `docs/e2e/task-claim-and-redelivery-checklist.md` §미실행 에
* **#978 부정 대조군 미실행** — `BSVIBE_WORKER_CLAUDE_CONFIG_DIR` 을 쓸 수 없는 경로로
  지정했을 때 `claude_config_dir_unavailable` 이 뜨고 런은 계속 완주하는지(fail-open).
  단위는 전선 절단 6건으로 덮였다
* **`bsvibe-shared-test-*` 17,721개** TMPDIR 누수 — 여전히 **이슈 미개설**

---

## §Ⅴ — 이 세션에서 값을 한 규율

* **⏳⭐⭐ 기각은 평결을 남기지만, 만료되는 건 그때의 조건이다.** 09-16 인수인계가
  *"#978 은 플래그로 못 닫는다고 측정 끝났다"* 로 그 트랙을 닫아 놨다. 실측 기각이었고
  **그날은 맞았다** — 진짜 이유는 워커 OAuth 가 만료돼 CLI 가 **호스트 크리덴셜 파일**로
  인증하고 있었고, 그 파일이 하필 옮기려는 디렉터리 안에 살았기 때문이다. 인증을 고친
  **다음 날 같은 레버가 그대로 동작했고**, 그건 prod 전체를 멈췄던 노출면을 닫는 레버였다.
  ⇒ **기각을 적을 땐 "X 인 동안은 못 한다"로 적어라.** 그리고 **선행조건을 닫았으면
  "이걸 이유로 기각됐던 게 뭔가"를 명시적으로 검색**해라 — 해소한 세션은 자기가 무엇을
  열었는지 모른다. 재측정은 **2×2** 로.
  (스킬 `a-rejection-records-a-verdict-but-the-reason-is-what-expires`)
* **⚙️⭐ 라이브러리를 아예 설정 안 하면 남의 기본값으로 돈다.** 워커가
  `configure_logging` 을 **한 번도 안 불러** prod 가 structlog 의 **개발용
  ConsoleRenderer + rich 예외**로 돌았다 — 예외 하나가 14줄·1909B, 로그 776MB 의 96.4%.
  그 기본값은 **개발자 터미널용으로 골라진 것**이다. 데몬이면 형식·레벨을 **명시**해라.
* **🔁⭐ 재시도를 누가 할 수 있는지는 "그때 입력을 누가 들고 있나"가 정한다.**
  옵션 비교표가 **타이밍 축으로만** 그려져 있으면 축이 하나 빠진 것이다. 그 축을 넣으면
  후보가 대개 하나로 줄고, **줄어든 쪽이 더 작고 더 안전하다.**
  (스킬 `who-still-holds-the-inputs-decides-who-can-retry`)
* **🔌⭐ 공유 함수의 가드는 호출자 배선을 증명하지 않는다.** 전선 절단 9건 중
  **2건이 초록**이었고 둘 다 호출 지점이었다. `await_completion` 의 *계산*은 지켰는데
  **정작 틀릴 수 있는 두 클로저는 무방비**였다. **호출자 N개면 테스트 N개.**
  (스킬 `a-guard-on-the-shared-function-proves-nothing-about-its-call-sites`)
* **🗄 마이그레이션의 테이블 이름은 SQLite 가 절대 못 잡는다.** `workers` 에 컬럼을
  추가하는 마이그레이션을 썼는데 **유닛 6363개가 전부 초록**이었다 —
  `create_all` 은 `__tablename__` 만 보고 마이그레이션을 **안 읽는다.** 진짜 이름은
  `executor_workers` 고, `workers` 는 0행인 채 2026-08-21 에 삭제됐다.
  ⇒ **fresh-PG 스모크가 이 부류를 잡는 유일한 게이트다.**
* **🧪 asyncio 테스트는 `await asyncio.sleep(0)` 없이 `create_task` 결과를 단언하면
  항상 통과한다.** 한 줄 넣자마자 같은 테스트가 `executed twice` 로 빨개졌다 —
  그 전까지 **실재하는 결함 위에서 초록**이었다.
* **📍 로그를 엉뚱한 컨테이너에서 찾으면 0건이 나온다.** awaiter 는
  `bsvibe-prod-worker-1` 에 산다. backend 만 보고 *"재전달이 안 걸렸다"* 로 한 번 오판했다.
  **0건은 "없다"가 아니라 "여기엔 없다"다.**
* **📏⭐ `tail -N` 으로 "이후"를 세지 마라.** 배포 후 로그 형식을 검증하면서 `tail -300`
  으로 ANSI 를 셌더니 **190** 이 나왔다 — 전부 **재기동 이전** 줄이었다. 한 번 오판했다.
  **경계 마커(`worker_starting`) 이후만** 세라. 시간이 아니라 **사건**으로 잘라야 한다.
* **🧟 워커를 멈추려면 `kill -STOP`, `launchctl stop` 이 아니다** — KeepAlive 가 되살린다.
  그리고 **identity 하나를 두 프로세스가 나눠 가질 수 있어**(#991) 하나만 멈추면 안 멈춘다.
