# E2E — 무료 플랜 동시 런 상한 (`workspaces.max_concurrent_runs`)

형님 결정 (2026-09-03): 무료 워크스페이스는 런을 **동시 3개**까지 보유한다.
`review_ready` 를 **포함**해서 센다. 초과 제출은 **제출 시점에 거절**하고, 거절
문구는 요금 페이지를 가리킨다.

> ⚠️ **이 문서의 `- [ ]` 는 "아직 안 걸어봤다"는 뜻이다 — "제품이 못 만든다"는
> 뜻이 아니다.** #879 에서 미체크 항목 둘이 실은 산출 불가능한 문장이었으므로,
> 아래 라이브 항목은 전부 *걸어보면 나올 수 있는 상태*인지 먼저 확인하고 적었다.
> 못 만드는 것이 발견되면 항목을 지우지 말고 **왜 못 만드는지와 함께** 고쳐 적어라.

---

## A. 유닛 / API — 자격증명 없이 돈다

- [x] `review_ready` 만 3개인 워크스페이스가 429 로 거절된다
      (`tests/api/test_run_cap.py::test_review_ready_runs_count_against_the_cap`)
      — ⭐ 이 기능의 존재 이유. prod 적체 103건이 **전부** `review_ready` 였다.
- [x] 상한 미만이면 202 로 수락된다
- [x] terminal(`shipped`/`failed`/`cancelled`) 런은 안 센다
- [x] `NULL` 상한은 무제한이다
- [x] 다른 워크스페이스의 적체는 내 예산을 안 쓴다
- [x] 거절 응답이 `{code: "run_cap_reached", limit: N}` 을 싣는다
- [x] 새 워크스페이스는 무료 상한으로 태어난다 (fail-open 아님)
- [x] MCP `bsvibe_direct` 도 같은 상한에 걸린다 — REST 만 막으면 우회된다
      (`tests/mcp/test_direct_run_cap.py`) + 상한 미만 통과 대조군
- [x] PWA 가 429 를 **사유로** 분기한다 — 제품이 있는 사용자에게
      "제품을 먼저 만드세요"를 안 보인다 (`apps/pwa/test/direct-run-cap.test.tsx`)
- [x] 문구의 숫자가 응답에서 온다 (limit 10 을 주면 10 이라 말한다)
- [x] 400(제품 0개) 안내는 그대로다 — 대조군
- [x] 요금 페이지가 무료 상한을 말한다 (`bsvibe-site tests/marketing.test.ts`)

**전선 절단 실증** — 각각 끊었을 때 *자기 것만* 빨개지는 것까지 확인:

| 끊은 것 | 빨강 | 초록 유지 |
|---|---|---|
| `review_ready` 를 카운트에서 제외 | 4 | 6 (미만·terminal·NULL·격리·MCP대조) |
| REST 배선 제거 | 3 | MCP 2개 전부 |
| MCP 배선 제거 | 1 | REST 7개 전부 |
| PWA 사유 분기 제거 | 4 | 400 대조군 |
| 사이트 문구 되돌림 | 3 | 나머지 25 |
| 마이그레이션 면제 UPDATE 제거 | 1 | — |

## B. 마이그레이션 — 진짜 Postgres 에서 돈다

- [x] fresh upgrade → full downgrade → re-upgrade 왕복
      (`tests/test_alembic_fresh.py::test_fresh_pg_upgrade_round_trip`, PG 16)
- [x] **면제 분기를 강제로 열었다** — 기존 워크스페이스는 3 으로 백필되고
      `admin@bsvibe.dev` 의 워크스페이스만 `NULL` 이 된다
      (`::test_run_cap_backfill_prices_everyone_but_the_operator`)
      — ⚠ 빈 DB 에서는 이 UPDATE 가 **0행에 걸려 no-op** 이라 왕복 테스트만으로는
      아무것도 증명되지 않는다. 행을 먼저 심어야 분기가 실행된다 (규율 104).

## C. 라이브 — prod 에서 실제로 걸었다 (MCP, 2026-09-03)

브라우저 하네스도 일회용 스택도 필요 없었다. **`bsvibe_direct` 가 이번에 상한을
태운 바로 그 표면**이고, MCP 는 진짜 prod 를 향한다. 대상은
`qazasa123's workspace` (cap 3, 정리 후 보유 0), 제품은 `bsvibe`.

- [x] 보유 0/3 · 1/3 · 2/3 에서 제출 → **전부 수락**
- [x] **보유 3/3 에서 제출 → 거절**
      `workspace already holds 3 concurrent runs (limit 3) — ship or discard a
      run that is waiting for review, then submit again`
- [x] ⭐ 런 하나를 discard → **거절당한 것과 글자 하나 다르지 않은 제출이 수락됐다.**
      코드도 빌드도 그대로고 **자리 하나만** 달라졌다. 상한이 무는 것과 자리가
      실제로 비는 것을 한 번에 증명한다
- [x] 문구가 **그 워크스페이스의 실제 상한**을 말한다 (하드코딩이 아니다)
- [x] probe 런 4개 전부 `discard_run` 으로 정리 — 워크스페이스는 **0/3 으로 원복**.
      외부 배달물 0건, 결정 2건 정상 해소

⚠️ **제출 사이마다 런이 실제로 생기기를 기다려야 한다.** 연달아 4번 보내면
§`run_caps.py` 에 적힌 **버스트 구멍**(아직 런이 안 된 제출은 안 센다) 때문에 4개
모두 통과하고, 상한이 고장난 것처럼 보인다. 검증 절차가 그 구멍을 밟으면 안 된다.

⚠️ **`client_attach` 제품을 쓰지 마라.** 그건 형님 로컬 워킹트리를 건드리고
취소해도 파일이 안 되돌아온다. `server_sandbox` 제품으로 하라 (여기서는 `bsvibe`).

### 아직 안 한 것

- [ ] `admin's workspace`(cap `NULL`, 보유 30)에서의 제출 — 에이전트의 MCP 가 그
      워크스페이스에 붙어 있지 않다. 면제 자체는 마이그레이션 실측(`cap = NULL`)과
      위 실험(같은 코드 경로가 라이브에서 돈다)으로 사실상 확인됐다
- [ ] **PWA 화면**에서의 거절 — 위는 MCP 표면이다. 문구·링크가 브라우저에서 어떻게
      보이는지는 별개이고 형님 자격증명이 필요하다 (§Ⅳ-b.1 Keychain)

## D. 배포 후 prod 실측

### 배포 전에 이미 한 것 (2026-09-03)

`qazasa123's workspace`(형님 본인 계정 — git author 동일)가 `review_ready` **73건**을
들고 있어 배포 즉시 잠길 참이었다. 형님 판정: **"정리하고 3으로"**.

- [x] 제품의 `discard_run` 으로 **73건 전부 정리** — 생 SQL 상태 변경이 **아니다**.
      결정·Safe Mode 항목을 해소하고 워크트리를 반환하며, **외부로 실제 배달된
      deliverable 7건은 일부러 회수하지 않고** 별도로 보고한다(`need_compensation=7`).
      결과: `discarded=73 · retracted=111 · need_compensation=7 · safe_mode_resolved=14`
- [x] 되돌릴 수 없는 작업이라 **1건 먼저** 돌려 결과 모양을 확인하고 나머지를 진행했다
- [x] 부수 효과: `/app/var/runs` **25GB/103디렉터리 → 1.3GB/30디렉터리** (~24GB 회수)
- [x] 정리 후 보유량: `qazasa123's` **0** · `bencharney234's` **0** · `admin's` 30(무제한 예정)

### 배포 후 (prod `24b933c`, 2026-09-03 실측)

- [x] `admin's workspace` 의 `max_concurrent_runs` 가 **`NULL`** 이다 (보유 30 — 잠기지 않음)
- [x] `qazasa123` / `bencharney234` 워크스페이스가 **`3`** 이다
- [x] **PWA 가 실제로 나갔다** — ⚠ `git_sha` 로는 알 수 없다(규율 109). 라이브 번들에
      `errorRunCap` 문장과 `errorRunCapLink: "See plans"` 가 들어 있는 것으로 확인했다
- [x] **마케팅 사이트가 실제로 나갔다** — `bsvibe.dev/{ko,en}/pricing` 이 새 문구를 서빙한다.
      ⚠ **머지 직후에는 옛 문구를 계속 서빙했다** — 전파를 눈으로 확인한 뒤에야 앱을
      머지했다. **머지 = 배포가 아니다** (메모리의 #528 전례)
- [x] 상한이 **prod 에서 실제로 문다** — §C 참조

```sql
SELECT w.name, w.max_concurrent_runs,
       count(r.id) FILTER (WHERE r.status NOT IN ('shipped','failed','cancelled')) AS held
FROM workspaces w LEFT JOIN execution_runs r ON r.workspace_id = w.id
GROUP BY w.id, w.name ORDER BY held DESC;
```

---

## 재현 명령

```bash
# A — 유닛/API (자격증명 불필요)
cd /Users/blasin/Works/bsvibe-app/main
uv run pytest tests/api/test_run_cap.py tests/mcp/test_direct_run_cap.py -q
cd apps/pwa && pnpm vitest run test/direct-run-cap.test.tsx
cd /Users/blasin/Works/bsvibe-site && pnpm test

# B — 마이그레이션 (진짜 PG 필요; 일회용 컨테이너로)
docker run -d --name bsvibe-migrate-probe \
  -e POSTGRES_USER=bsvibe -e POSTGRES_PASSWORD=bsvibe -e POSTGRES_DB=bsvibe \
  -p 15452:5432 pgvector/pgvector:pg16
export BSVIBE_MIGRATION_DATABASE_URL="postgresql+asyncpg://bsvibe:bsvibe@localhost:15452/bsvibe"
export BSVIBE_APP_DB_PASSWORD=probeprobe
uv run alembic upgrade head            # 이게 least-privilege bsvibe_app 롤을 만든다
# ⚠ 런타임 DSN 은 owner 가 아니라 bsvibe_app 이어야 한다 — owner 로 두면
#   test_rls_is_active_layer3_for_the_runtime_role 이 "RLS 는 superuser 에게 무력"
#   이라며 정당하게 빨개진다 (실측 2026-09-03).
export BSVIBE_DATABASE_URL="postgresql+asyncpg://bsvibe_app:probeprobe@localhost:15452/bsvibe"
uv run pytest tests/test_alembic_fresh.py -q
docker rm -f bsvibe-migrate-probe      # ⚠ 끝나면 반드시
```

⚠️ **PG 없이 돌린 게이트는 `43 skipped` 로 초록이었고 이 마이그레이션이 그 안에
있었다.** 스키마를 건드리는 PR 은 게이트 요약의 **skip 개수를 먼저 읽어라** — PG 를
켜면 43 → 1 로 떨어진다.

⚠️ **15442 는 쓰지 마라** — `devcontainer-postgres-1` 이 3주째 잡고 있고, 이
스위트는 `DROP SCHEMA` 로 시작한다. 목적지가 **비어 있는지 먼저 확인**하라
(`SELECT count(*) FROM information_schema.tables WHERE table_schema='public'`).
