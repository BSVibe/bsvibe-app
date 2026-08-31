# `runs` 를 제품으로 좁히는 축 — 검증 체크리스트

> 2026-09-01. 인수인계 §Ⅳ.5 *"MCP 는 전체를 받아 클라이언트 측 필터. REST 에는
> 파라미터가 아예 없다."* 재보니 이건 **누락이 아니라 결함**이었다 — 세 표면
> 모두 조용히 틀린 답을 주고 있었다.

## 무엇이 틀렸나

`SqlAlchemyRunRepository.list_by_product` 는 **이미 있었다** (테스트도 있다,
`tests/workflow/infrastructure/repositories/test_run_repository_list_by_product.py`).
쓰는 곳은 `product_tick_planner` 하나뿐이었고, 사용자에게 보이는 세 표면은 전부
**워크스페이스 전체를 한 페이지 받아 자른 뒤** 제품으로 걸렀다.

| 표면 | 이전 | 결과 |
|---|---|---|
| `bsvibe_runs_list(product_slug_or_id=…)` | `list_by_workspace(limit)` → 파이썬 필터 | 다른 제품의 새 런이 `limit` 개를 채우면 **`[]`** |
| `GET /api/v1/runs` | 파라미터 **없음** | 좁힐 방법 자체가 없음 |
| PWA 제품 상세 | `listRuns(100)` → `runs.filter(...)` | 워크스페이스가 바쁘면 제품 이력이 **빈 화면** |

⚠️ **셋 다 에러를 내지 않는다.** "이 제품엔 런이 없습니다"라고 **차분하게** 답한다.
런이 있는데도. 자르기가 좁히기보다 **먼저** 일어나는 한, 답은 그 제품이 아니라
*워크스페이스가 얼마나 바빴는가*에 대한 것이다.

## 새 형태

좁히기를 **쿼리에서** 한다 — 세 표면 모두 이미 있던 `list_by_product` 로.

* `GET /api/v1/runs?product_id=<uuid>` (신규 파라미터, 없으면 종전과 동일)
* `_h_runs_list` → 슬러그/ID 해석 후 `list_by_product`
* `getProductDetail` → `listRuns(runLimit, product.id)`
  * ⚠️ `listProducts()` 와의 `Promise.all` 을 **순차로 바꿨다**. 슬러그를 풀기
    전에는 product id 가 없다. 왕복 1회를 더 쓰고 정답을 얻는 쪽을 골랐다.

## 가드 (실패를 먼저 봤다 — 전부 RED 확인)

| 테스트 | 명제 | RED 일 때 |
|---|---|---|
| `tests/mcp/test_workflow_tools.py::test_runs_list_by_product_is_not_truncated_by_newer_other_runs` | 제품의 런은 워크스페이스가 얼마나 바빴는지와 무관하게 나온다 | `[] != ['<run>']` |
| `tests/api/test_v1_db_routes.py::test_runs_list_filters_by_product` | `product_id` 가 쿼리에서 좁힌다 | 파라미터가 **무시됨** (무관한 런 2건 반환) |
| `apps/pwa/test/product-detail-client.test.ts` (신규 케이스) | 브라우저가 **서버에** 좁히기를 요청한다 | `[] != ['r-mine']` |

⚠️ 픽스처의 `created_at` 은 **명시**했다 (`now()` 아님). 순서 자체가 검사 대상이라
시드가 얼마나 빨리 돌았는지에 답이 달리면 안 된다.

⚠️ 공유 `mockFetch` 가 `product_id` 를 **무시하고** 있었다 (`run_id` 는 이미 존중).
목이 엔드포인트 계약을 안 지키면 클라이언트측 필터 회귀가 영원히 통과한다. 목을
고쳤지 단언을 약화시키지 않았다.

## 체크리스트

- [ ] `.venv/bin/pytest tests/mcp/test_workflow_tools.py tests/api/test_v1_db_routes.py -q` → green
- [ ] `.venv/bin/lint-imports` → 5 kept, 0 broken (새 예외 **0건**)
- [ ] `.venv/bin/mypy backend/` → clean
- [ ] `pnpm lint && pnpm typecheck && pnpm test && pnpm build` (apps/pwa)
- [ ] 배포 후 prod 실측: 런이 있는 제품에 대해 `product_id` 로 좁힌 결과가 비지 않는다
