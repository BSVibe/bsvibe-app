# BSVibe 세션 인수인계 — 2026-09-17

**prod 코드**: `2c50ff9` (#990 — claim 기반 재전달. 마이그레이션 있음, 워커 재시작 필요했음)
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

1. **🔓 #978 하네스 교체 — 차단이 풀렸다** ⭐ — §Ⅲ.1 워커 OAuth 재로그인이 오늘
   끝났으므로 `CLAUDE_CONFIG_DIR` + BSVibe 소유 config dir 로 가도 생명줄이 안 끊긴다.
   플래그로는 못 닫는다고 09-16 에 측정 끝났다
2. **🔴 codex·opencode 가 원칙을 위반한다 — 형님 결정 대기** — `_REMOTE_TOOL_EXECUTORS`
   가 `claude_code` 하나다. 선택은 **MCP 표면 부여 + 네이티브 박탈** 또는 **agentic 제외**
3. **#991 워커 identity** — 위 §Ⅱ
4. **#970 로그 소음** — 발생원은 멈췄지만 3항목 그대로(트레이스백→한 줄 · 백오프 · 245MB 로테이션).
   30분짜리
5. **#957 KMS key-id** · **#964 settle 스코프** · **#928 과금**(**#953 이전 숫자로 튜닝 금지**)

### 검증 안 된 것 (정직하게)

* **긴 `act` 턴이 재전달되지 않는지 prod 에서 못 봤다** — 검증 런이 전부 수 초짜리였다.
  **이 설계가 lease 대신 claim 을 고른 유일한 이유라 한 번은 봐야 한다**
* **#966 체크리스트 3번**(긴 `act` 턴이 안 잘리는지) — 같은 이유로 여전히 미검증.
  **다음 긴 런에서 반드시 확인할 것**(`agent_loop.act` 는 3600s)
* `client_attach` verify 게이트 회귀 · claim 500 fail-open · `cancel` 경로 —
  전부 단위로만 덮였다. 이유는 `docs/e2e/task-claim-and-redelivery-checklist.md` §미실행 에
* **`bsvibe-shared-test-*` 17,721개** TMPDIR 누수 — 여전히 **이슈 미개설**

---

## §Ⅴ — 이 세션에서 값을 한 규율

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
* **🧟 워커를 멈추려면 `kill -STOP`, `launchctl stop` 이 아니다** — KeepAlive 가 되살린다.
  그리고 **identity 하나를 두 프로세스가 나눠 가질 수 있어**(#991) 하나만 멈추면 안 멈춘다.
