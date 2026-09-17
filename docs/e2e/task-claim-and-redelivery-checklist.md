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

## 배포 전 — 베이스라인

- [ ] 현재 `dispatched` 로 굳어 있는 행 수를 **기록**한다 (이게 베이스라인)
      `SELECT count(*) FROM executor_tasks WHERE status='dispatched';`
- [ ] 마이그레이션 head 가 **하나**인지 확인 — 머지 전 `down_revision` 대조
      (열린 PR 이 없어도, 머지 시점에 다른 마이그레이션이 들어왔으면 갈라진다)

## 배포 후 — 스키마·프로토콜

- [ ] `alembic upgrade head` 가 prod PG 에서 통과한다
- [ ] `executor_tasks.claimed_at` 이 **nullable** 이고 기존 행이 전부 NULL 이다
      (fresh PG 스모크는 이 PR 에서 이미 돌렸다 — `upgrade head` · `downgrade -1` 왕복 통과)
- [ ] `executor_workers.protocol_version` 기본값이 **1** 이다 (옛 워커 = 재전달 제외)
      ⚠️ 테이블 이름은 `workers` 가 아니라 **`executor_workers`** 다. `workers` 는 한 행도
      안 들어간 채 2026-08-21 `drop_dead_worker_tables` 로 삭제됐고, **SQLite 유닛
      테스트는 `__tablename__` 대로 만들어 주므로 틀린 이름도 전부 초록이었다**
- [ ] 워커 3개 kickstart → `ps -eo pid,lstart` 로 시작 시각이 방금인지 확인
- [ ] 재시작 후 하트비트 한 번 돈 뒤 세 워커의 `protocol_version` 이 **2** 로 올라간다
      `SELECT name, protocol_version, last_heartbeat FROM executor_workers ORDER BY last_heartbeat DESC;`
      ⚠️ 이게 **양성 대조군**이다 — 여기가 1로 남아 있으면 헤더가 도달하지 않은 것이고,
      그러면 **재전달은 영원히 안 걸린다**(그리고 아무 에러도 안 난다)

## 동작 — 정상 경로

- [ ] PWA 에서 런을 하나 쏜다 → `review_ready` 까지 완주한다
- [ ] 그 태스크 행의 `claimed_at` 이 **채워져 있다**
- [ ] `claimed_at` 과 `created_at` 의 간격이 **초 단위**다 (분 단위면 폴링이 느린 것)
- [ ] 워커 로그에 `task_claimed` 가 태스크마다 **정확히 한 줄**
- [ ] 백엔드 로그에 `executor_task_redelivered` 가 **없다** (정상 런은 재전달 안 됨)

## 동작 — 긴 턴이 중복되지 않는다 ⭐

이 PR 이 lease 방식 대신 claim 을 고른 **유일한 이유**다. 여기가 깨지면 설계가 틀린 것.

- [ ] `agent_loop.act` 가 도는 긴 런(수 분 이상)을 하나 건다
- [ ] 그 태스크가 **재전달되지 않는다** — `executor_task_redelivered` 로그 0건
- [ ] 워커에 같은 `task_id` 의 execute 메시지가 **한 번만** 도착한다
- [ ] ⚠️ **#966 체크리스트 3번을 여기서 같이 본다** — 긴 `act` 턴이 안 잘리는지.
      프레이밍 행이 죽어 있던 동안 못 봤고, 지금은 볼 수 있다

## 동작 — 유실이 실제로 복구된다 ⭐

- [ ] 워커를 **정지**시킨 채(`launchctl stop`) 런을 하나 쏜다 →
      행이 `dispatched` + `claimed_at IS NULL` 로 앉는다
- [ ] 워커를 다시 띄운다 → `executor_task_redelivered` 가 뜨고 런이 **완주**한다
- [ ] 재전달 페이로드의 `timeout_s` 가 **원래 값보다 작다** (남은 마감 — F10)
      백엔드 로그의 `executor_task_redelivered remaining_s=` 로 확인
- [ ] 재전달은 **한 번만** 일어난다 (같은 태스크에 두 줄이 안 찍힌다)
- [ ] 영수증 조회도 **한 번만** 일어난다 — 긴 턴에서 폴링 질의가 2배로 늘지 않는다

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
- [ ] `claimed_at` 을 손으로 NULL 로 만들면 긴 턴도 재전달 후보가 된다
      (= 이 설계의 유일한 축이 `claimed_at` 이라는 증거)
