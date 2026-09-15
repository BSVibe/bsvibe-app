# E2E 체크리스트 — 배경 경로 워크스페이스 스코프 (#959)

**대상**: `workspace_scope` (레이어 2) + `after_begin` GUC 리스너 (레이어 3) + AgentWorker 드라이브 배선

> ⚠️ **dev compose 는 owner 역할**(`deploy/compose.yaml:53`)이라 **로컬에서 RLS 는 검증되지 않는다.**
> 아래 RLS 항목은 (a) 자기 역할을 직접 만드는 테스트(`test_rls_pg.py`), (b) CI(`bsvibe_app`),
> (c) prod 에서만 진짜로 측정된다. 로컬 초록은 RLS 에 대해 아무 말도 하지 않는다.

## 기계적 검증 (테스트가 대신함)

- [x] `uv run pytest tests/data/test_workspace_guc_publication.py` — 퍼블리케이션 역학
- [x] `uv run pytest tests/data/test_rls_pg.py -k background_scope` — fail-open → 격리 → fail-open (비수퍼유저 역할)
- [x] `uv run pytest tests/glue/test_background_workspace_scope.py` — 드라이브 배선 + 클레임은 전역
- [x] `uv run pytest tests/architecture/test_tenant_table_ratchet.py` — 드리프트 래칫
- [x] 전선 절단: 리스너 등록을 지우면 `background_scope` 테스트가 **격리 실패**로 빨개진다
      (복원 후 해시 재검증). 래칫 핀 한 줄 제거 → 2건 빨강. `workspace_scope` 의 rls 임포트 제거 →
      레이어 3 설치 테스트 빨강. 잘못된 workspace 를 publish → `test_drive_failure_reaches_the_founder` 4건 빨강.
- [x] 전체 스위트: SQLite **6297 passed** · probe PG(`bsvibe_app`, NOSUPERUSER NOBYPASSRLS)
      production/data/auth/identity/api **821 passed** · glue/workflow/schedule/executors/dispatch/architecture **2263 passed**

## 배포 후 prod 에서 직접 볼 것

- [ ] **런이 여전히 돈다** — 스코프가 클레임으로 새면 다른 워크스페이스의 런이 조용히 멈춘다.
      `execution_runs` 에서 `status='open'` 이 배포 시각 이후 **쌓이지 않는지**. 워크스페이스가
      둘 이상일 때만 의미가 있다(단일 워크스페이스는 음성 대조군이 못 된다).
- [ ] **드라이브가 완주한다** — 배포 후 최소 한 건의 런이 `review_ready`/`done` 에 도달.
      레이어 2 가 드라이브 중 필요한 행을 가렸다면 여기서 터진다.
- [ ] **GUC 잔류 0** — prod PG 에서
      `SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '%bsvibe%'` 로 살아 있는
      커넥션을 확인한 뒤, 백엔드에 무작위 요청을 섞어 보내고 **가입 경로**(`POST /api/auth/login`)가
      간헐 실패하지 않는지. (#959 §4 의 잠재 결함 — 이 변경이 구조적으로 닫는다)
- [ ] **워커 프로세스가 새 코드다** — `launchctl kickstart -k` 후 **프로세스 시작 시각**으로 확인.
      호스트 워커는 autodeploy 되지 않는다.

## 이 변경이 하지 않는 것 (다음 PR)

- [ ] `schedule`(`db_poll_runner.py:98-113`) / `delivery`(`delivery_worker.py:409-489`) — 한
      트랜잭션에 **여러 워크스페이스의 행을 배치**하고 마지막에 한 번 커밋한다. 항목별로 스코프를
      두르면 마지막 워크스페이스의 GUC 아래에서 배치 전체가 flush 된다. **트랜잭션을 항목별로
      쪼갠 뒤에야** 같은 배선이 가능하다.
- [ ] `settle`(`settle_worker.py:948-970`) — 이쪽은 **행마다 커밋**하므로 항목별 스코프가 지금
      구조에서도 가능하다. 다음 PR 후보 1순위.
