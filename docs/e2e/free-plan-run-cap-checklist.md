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

## C. 라이브 — 스택 + 브라우저

> ⚠️ **아래는 아직 한 줄도 안 걸었다.** A 가 전부 초록인 것은 *이 절이 통과했다는
> 뜻이 아니다* — 두 절은 서로 다른 것을 증명한다. A 는 규칙과 표면을, C 는 둘이
> 실제 HTTP 위에서 만나는지를 증명한다.

전제: 라이브 스택 + 형님 자격증명(§Ⅳ-b.1 Keychain). 재현은
`docs/e2e/authenticated-shell-live-checklist.md` 와 같은 스택을 쓴다.

- [ ] 워크스페이스에 `review_ready` 런 3개를 심고 PWA 에서 요청을 보내면
      거절 문구가 뜨고 **숫자 3이 그 문구 안에 있다**
- [ ] 그 문구의 "플랜 보기" 링크가 `https://bsvibe.dev/{locale}/pricing` 를 열고,
      **그 페이지가 같은 숫자를 말한다** (링크가 자기를 보낸 거절과 모순되지 않는다)
- [ ] 런 하나를 ship/discard 하면 다음 요청이 통과한다 (자리가 실제로 빈다)
- [ ] 상한이 `NULL` 인 워크스페이스는 3개를 넘겨도 계속 통과한다

## D. 배포 후 prod 실측

- [ ] `admin's workspace` 의 `max_concurrent_runs` 가 `NULL` 이다
- [ ] `qazasa123` / `bencharney234` 워크스페이스가 `3` 이다
- [ ] 형님이 요청을 보낼 수 있다 (면제가 실제로 먹었다는 유일한 증거)

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
export BSVIBE_DATABASE_URL="$BSVIBE_MIGRATION_DATABASE_URL"
uv run pytest tests/test_alembic_fresh.py -q
docker rm -f bsvibe-migrate-probe      # ⚠ 끝나면 반드시
```

⚠️ **15442 는 쓰지 마라** — `devcontainer-postgres-1` 이 3주째 잡고 있고, 이
스위트는 `DROP SCHEMA` 로 시작한다. 목적지가 **비어 있는지 먼저 확인**하라
(`SELECT count(*) FROM information_schema.tables WHERE table_schema='public'`).
