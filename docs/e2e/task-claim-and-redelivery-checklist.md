# E2E — claim 이 수신 증거이고, 안 claim 된 것만 재전달된다 (#965)

**대상 PR**: executor 태스크 claim + awaiter 재전달
**전제**: 호스트 워커는 **autodeploy 안 된다.** 배포 후 반드시
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 하고,
**프로세스 시작 시각**으로 새 코드인지 확인할 것.

> ⚠️ **배포 순서가 있다.** 백엔드가 먼저다 — 워커가 보내는 `X-BSVibe-Worker-Protocol`
> 헤더와 `POST /api/v1/workers/claim` 을 옛 백엔드는 모른다. 워커를 먼저 올리면
> claim 이 404 로 떨어지고, 그건 **fail-open 경로**라 조용히 예전처럼 돈다(무해하지만
> 그 창에서는 재전달이 안 걸린다).

> ⚠️ **`extra="forbid"` 때문에 바디가 아니라 헤더다.** 프로토콜 버전을 `HeartbeatBody`
> 에 넣으면 **옛 백엔드가 422 로 모든 하트비트를 거절**한다 — 워커가 통째로 오프라인이
> 된다. 헤더는 모르는 쪽이 그냥 무시한다.

---

> ✅ **2026-09-17 배포에서 전량 실행했다.** prod `2c50ff9`. 미체크로 남은 항목은
> 아래 §미실행 에 이유와 함께 모아 뒀다 — 체크 안 된 칸이 "확인 안 함"인지
> "못 함"인지 구분되지 않으면 다음 사람이 같은 자리에서 또 멈춘다.

## 배포 전 — 베이스라인

- [x] 현재 `dispatched` 로 굳어 있는 행 수를 **기록**한다 (이게 베이스라인)
      `SELECT count(*) FROM executor_tasks WHERE status='dispatched';`
      → **0** (total 7112, workers 8)
- [x] 마이그레이션 head 가 **하나**인지 확인 — 머지 전 `down_revision` 대조
      → prod 가 `workspace_token_budget` 이었고 `down_revision` 과 일치

## 배포 후 — 스키마·프로토콜

- [x] `alembic upgrade head` 가 prod PG 에서 통과한다 → `alembic_version = task_claim_receipt`
- [x] `executor_tasks.claimed_at` 이 **nullable** 이고 기존 행이 전부 NULL 이다
      → `timestamptz null=YES default=-`
      (fresh PG 스모크는 이 PR 에서 이미 돌렸다 — `upgrade head` · `downgrade -1` 왕복 통과)
- [x] `executor_workers.protocol_version` 기본값이 **1** 이다 (옛 워커 = 재전달 제외)
      → `integer null=NO default=1`
      ⚠️ 테이블 이름은 `workers` 가 아니라 **`executor_workers`** 다. `workers` 는 한 행도
      안 들어간 채 2026-08-21 `drop_dead_worker_tables` 로 삭제됐고, **SQLite 유닛
      테스트는 `__tablename__` 대로 만들어 주므로 틀린 이름도 전부 초록이었다**
- [x] 워커 3개 kickstart → `ps -eo pid,lstart` 로 시작 시각이 방금인지 확인
      → 16:00:18, 새 PID 1048/1051/1054
- [x] 재시작 후 하트비트 한 번 돈 뒤 **살아 있는 identity 전부**의 `protocol_version` 이 **2** 다
      → `mac-mini-e2e` · `admin-test-exec` 둘 다 v2
      🚨 **"세 워커"가 아니라 둘이다.** `com.bsvibe.worker` 와
      `com.bsvibe.worker-mac-mini-e2e` 는 **같은 `~/.bsvibe/worker.token` 을 읽어
      하나의 identity 로 돈다**(#991). 데몬 수로 세면 영원히 하나가 모자라 보인다 —
      **행 수가 아니라 `last_heartbeat` 가 신선한 행**으로 세라
      `SELECT name, protocol_version, last_heartbeat FROM executor_workers ORDER BY last_heartbeat DESC;`
      ⚠️ 이게 **양성 대조군**이다 — 여기가 1로 남아 있으면 헤더가 도달하지 않은 것이고,
      그러면 **재전달은 영원히 안 걸린다**(그리고 아무 에러도 안 난다)

## 동작 — 정상 경로

- [x] 런을 하나 쏜다 → `review_ready` 까지 완주한다 → 런 `b737f9a6`
- [x] 그 태스크 행의 `claimed_at` 이 **채워져 있다**
- [x] `claimed_at` 과 `created_at` 의 간격이 **초 단위**다 (분 단위면 폴링이 느린 것)
      → 0.17s · 2.57s · 4.59s · 0.99s
- [x] 워커 로그에 `task_claimed` 가 태스크마다 **정확히 한 줄**
      ⚠️ **#970 이후 워커 로그는 JSON 이다.** 아래 형태로 읽어라 —
      2026-09-17 검증 당시의 콘솔 형식 grep 은 더 이상 안 맞는다:
      `jq -r 'select(.event|startswith("task_claim")) | "\(.timestamp) \(.event) \(.task_id)"' ~/Library/Logs/bsvibe-worker*.log`
- [x] 정상 런에 `executor_task_redelivered` 가 **없다**
      ⚠️ **로그는 `bsvibe-prod-worker-1` 에 있다, backend 가 아니다.** awaiter 는
      런 오케스트레이터 쪽에 산다 — backend 컨테이너만 보면 **0건으로 오판한다**
      (이번에 실제로 한 번 밟았다)

## 동작 — 긴 턴이 중복되지 않는다 ⭐

이 PR 이 lease 방식 대신 claim 을 고른 **유일한 이유**다. 여기가 깨지면 설계가 틀린 것.

- [ ] `agent_loop.act` 가 도는 긴 런(수 분 이상)을 하나 건다
- [ ] 그 태스크가 **재전달되지 않는다** — `executor_task_redelivered` 로그 0건
- [ ] 워커에 같은 `task_id` 의 execute 메시지가 **한 번만** 도착한다
- [ ] ⚠️ **#966 체크리스트 3번을 여기서 같이 본다** — 긴 `act` 턴이 안 잘리는지.
      프레이밍 행이 죽어 있던 동안 못 봤고, 지금은 볼 수 있다

## 동작 — 유실이 실제로 복구된다 ⭐

⚠️ **`launchctl stop` 으로는 안 된다** — KeepAlive 가 되살리고, 무엇보다 **한
identity 를 두 프로세스가 나눠 갖고 있어**(#991) 하나만 멈추면 다른 하나가 계속
일한다. 실측에서 pid 1054 를 얼렸는데 런이 그대로 완주했다.
⇒ **`kill -STOP <그 identity 의 모든 pid>`.** 얼린 프로세스는 launchd 가 재시작하지
않고, 하트비트만 멈춘다. 신선도 120s · 재전달 유예 30s 라 창은 충분하다.

- [x] 워커를 **얼린** 채 런을 하나 쏜다 → 행이 `dispatched` + `claimed_at IS NULL`
      → 태스크 `710b4b52`, 07:07:03 생성
- [x] 워커를 되살린다 → `executor_task_redelivered` 가 뜨고 런이 **완주**한다
      → 런 `593dd849` **`review_ready`**. claim 까지 102.83s — 이 변경 전이라면
      300s 타임아웃으로 실패했을 런이다
- [x] 재전달 페이로드의 `timeout_s` 가 **원래 값보다 작다** (남은 마감 — F10)
      → `remaining_s=269.7` / 예산 300s. 유예는 07:07:03→07:07:33 **정확히 30s**
- [x] 재전달은 **한 번만** 일어난다 → `redelivered` 1줄 + `redispatched` 1줄
- [x] **중복이 실제로 걸러진다** — 원본과 재전달본이 같은 poll 배치로 도착했고
      `task_duplicate_dropped` 1 + `task_claimed` 1. 백엔드에 묻기도 전에 워커
      자체 dedupe 가 걸렀다
- [ ] 영수증 조회도 **한 번만** — 로그로는 안 보인다(질의에 로그가 없다). 유닛으로
      핀 박아 뒀다: `test_the_receipt_is_checked_once_not_on_every_poll`

## 회귀 — 이번 변경이 깰 수 있는 것

- [ ] **client_attach 경로**(`client_worker_manager`)의 verify 게이트가 그대로 돈다.
      이 경로는 `env` 로 시크릿을 넘긴다 — 재전달이 그 값을 **로그·DB·스트림 어디에도
      남기지 않는지** 확인할 것
- [ ] claim 이 실패(500/타임아웃)해도 워커가 태스크를 **그냥 실행한다**(fail-open).
      전선 끊기: 백엔드 claim 라우트를 잠깐 500 으로 만들고 런이 완주하는지
- [ ] 결과 보고가 그대로 200 이다 — `record_result` 의 `status='dispatched'` 검사는
      건드리지 않았다
- [ ] 취소(`cancel`) 경로가 그대로 동작한다 — `_RUNNING_TASKS` 조회를 공유한다

## 부정 대조군 — 안 걸리면 이 가드는 아무것도 안 재고 있는 것

- [ ] `executor_workers.protocol_version` 을 1 로 손으로 내리고 유실을 재현하면
      **재전달이 안 일어난다**. 다시 2 로 올리면 **일어난다**
      → **미실행**(아래 §미실행 참조). 단위 쪽에서는 전선 절단으로 증명돼 있다:
      `_is_unreceived` 의 버전 게이트를 지우면
      `test_a_worker_that_cannot_claim_is_never_redelivered_to` 가 빨개진다
- [ ] `claimed_at` 을 손으로 NULL 로 만들면 긴 턴도 재전달 후보가 된다
      (= 이 설계의 유일한 축이 `claimed_at` 이라는 증거) → **미실행**

---

## §미실행 — 왜 안 걸었는지 (빈 칸을 남기지 않기 위해)

체크 안 된 칸이 *"아직 안 봄"* 인지 *"못 봄"* 인지 구분되지 않으면 다음 사람이 같은
자리에서 또 멈춘다. 2026-09-17 배포 시점 기준:

| 항목 | 왜 안 걸었나 | 대신 무엇이 지키나 |
|---|---|---|
| 부정 대조군 2건 | **prod DB 에 손으로 UPDATE 를 쳐야 한다.** 형님 승인 필요 — 특히 `protocol_version=1` 을 걸어 두고 복구를 잊으면 **재전달이 영원히 안 걸리고 아무 에러도 안 난다** (워커가 다음 하트비트에 2 로 되돌리므로 자가복구되긴 한다) | 전선 절단 10건이 단위에서 전부 하나씩 빨개진다 |
| **긴 `act` 턴이 중복되지 않는다** ⭐ | 검증 런이 전부 **수 초짜리**였다. 수 분~1시간 도는 실제 작업 런이 필요하다 | `claimed_at IS NULL` 이 유일한 축이므로 claim 된 행은 길이와 무관하게 후보가 아니다 — 그러나 **이 설계가 lease 대신 claim 을 고른 유일한 이유라 prod 에서 한 번은 봐야 한다** |
| **#966 체크리스트 3번**(긴 `act` 턴이 안 잘리는지) | 위와 같은 이유 | 없음. **다음 긴 런에서 반드시 확인할 것** |
| `client_attach` verify 게이트 회귀 | 그 경로를 타는 런을 안 걸었다 | 단위: `test_the_exec_retry_resends_the_same_env_with_the_remaining_budget` |
| claim 500 fail-open | 백엔드 라우트를 일부러 깨야 한다 | 단위: `test_a_claim_transport_error_does_not_silently_drop_the_task` + 전선 절단 F |
| `cancel` 경로 회귀 | 취소할 런을 안 만들었다 | `_RUNNING_TASKS` 조회를 공유할 뿐 분기를 안 건드렸다 |

### 이번에 실측으로 정정된 전제

* **로그는 `bsvibe-prod-worker-1` 에 있다** — awaiter 가 거기 산다. backend 만 보면 0건
* **워커 identity 는 데몬 수와 다르다** — #991. 행 수가 아니라 신선한 `last_heartbeat` 로 세라
* **워커를 멈추려면 `kill -STOP`**, `launchctl stop` 이 아니다
* **#970 이후 워커 로그는 JSON 한 줄이다** — 이 문서의 로그 예시 중 콘솔 형식
  (`2026-09-17 16:08:46 [info ] task_claimed task_id=…`)은 **2026-09-17 검증 당시의
  것**이고, 지금 다시 걸면 `jq` 로 읽어야 한다
