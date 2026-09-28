# E2E 체크리스트 — RLS 명시적 교차테넌트 읽기 (#959 ①)

**무엇이 바뀌나**: 정책 `rls_workspace_isolation` 의 `USING` 에 `guc = '*'` 가 더해지고,
`backend.data.rls.cross_tenant_read()` 가 생긴다. **아직 아무도 부르지 않는다** — 11곳 전환은 ②.
그러니 prod 에서 볼 것은 *"정책이 바뀌었다"* 와 *"아무것도 안 바뀌었다"* 둘이다.

## 로컬 (일회용 PG, `bsvibe_app` 런타임 역할)

- [x] `'*'` 로 두 테넌트가 다 보인다 — `test_the_star_guc_opens_reads_across_tenants`
- [x] `'*'` 로 쓰기는 거절된다 — `test_the_star_guc_does_not_open_writes`
- [x] 컨텍스트 안에서 열린 트랜잭션만 `'*'`, 나오면 빈 GUC — `test_cross_tenant_read_publishes_…`
- [x] 안쪽 테넌트 스코프가 이긴다 — `test_a_tenant_scope_inside_it_is_the_narrower_one_and_wins`
- [x] 전선 절단 3개가 각각 자기 테스트만 뒤집는다 (WITH CHECK 에 `'*'` · 리스너 게시 제거 · 우선순위 역전)
- [x] `alembic downgrade -1` → `upgrade head` 왕복

## prod (배포 후, 읽기 전용)

- [ ] **정책이 바뀌었다** — 6/6 정책의 `qual` 에 `'*'` 가 있고 `with_check` 에는 **없다**
  ```sql
  SELECT count(*) FILTER (WHERE qual LIKE '%''*''%' AND with_check NOT LIKE '%''*''%') || '/' || count(*)
  FROM pg_policies WHERE policyname = 'rls_workspace_isolation';
  ```
- [ ] **아무것도 안 바뀌었다** — 배포 이후에 생긴 런이 클레임됐다(`claimed_by` 채워짐 또는 `open` 을 벗어남).
  분모를 먼저: 배포 이후 생성된 런이 0 이면 이 칸은 **판정 불가**로 적는다
- [ ] 백엔드·워커 로그에 `row-level security` 에러가 배포 전보다 늘지 않았다
