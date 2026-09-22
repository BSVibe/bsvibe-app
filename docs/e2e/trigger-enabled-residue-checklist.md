# E2E 체크리스트 — `trigger.enabled` 잔재 3층 (#1003 별건)

**대상**: #924 가 `trigger.enabled` 를 지운 뒤에도 **prod 가 그 키를 계속 만들고 있던** 경로 세 개.
**배포 전 실측(2026-09-22)**: prod `resource_bindings` **3행 전부** `{"enabled": false, "filters": {}}`.
그중 한 행은 삭제 **일주일 뒤인 09-18 04:00 UTC** 에 태어났다 — 삭제가 안 끝나 있었다는 뜻이다.

> 🧭 **각 항목은 반대 판정이 가능해야 한다.** 이 문서의 절단 결과(§D)가 그 증거다.

## A. 배포 전 (로컬 — 완료)

- [x] REST 가 `trigger.enabled` 를 거절한다 — `ResourceBindingUpdate(trigger={'enabled': True, ...})`
      → `ValidationError`. **대조군**: `{'filters': {}}` 는 통과
- [x] MCP 가 **디스패처 홉에서** 거절한다 — `registry.call_tool("bsvibe_bindings_update", …)`
      → `ToolError: extra_forbidden … ('trigger','enabled')`. **대조군**: `{"filters": {...}}` 는 통과
- [x] MCP 와 REST 가 **같은 클래스**를 쓴다 — `backend/common/binding_knobs.py:TriggerKnob`.
      사본이 아니므로 다시 어긋날 자리가 없다. `lint-imports` 6 계약 전부 KEPT
- [x] 에이전트가 읽는 **와이어 설명**에 죽은 키가 없다 — `registry.list_tools()` 의
      description + inputSchema 를 실제로 읽어 확인(소스 grep 아님)
- [x] 일회용 PG 에서 `alembic upgrade head` → `resource_bindings.trigger` 의 DDL 기본값에
      `enabled` 없음. **대조군**: `downgrade` 하면 옛 기본값이 돌아와 같은 검사가 빨개진다
- [x] PWA 유닛 802건 통과 — 죽은 노브의 체크박스가 렌더되지 않는다
- [x] CI 명령 **다섯 + PWA 넷** 전부 로컬 재현: ruff check · ruff format --check ·
      lint-imports · mypy backend/ · pytest(7122 passed) · biome · tsc · vitest · next build

## B. 배포 후 (prod `92392bd`, 컨테이너 재시작 2026-09-22 — 실측 완료)

- [x] `alembic_version` = `trigger_default_no_dead_key`
- [x] **DDL 기본값**: `'{"filters": {}}'::jsonb` — 배포 전에는
      `'{"enabled": false, "filters": {}}'::jsonb` 였다
- [x] **기존 행**: 죽은 키를 든 행 **3 / 3 → 0 / 3**. 분모를 같이 적는다 —
      배포 전 기준선을 안 남겼으면 이 0 은 *"원래 없었다"* 와 구분이 안 된다
- [x] **살아 있는 절반이 안 죽었다**: 세 행 전부 `{"filters": {}}`
- [x] **배포된 백엔드가 광고하는 메뉴**에 죽은 키가 없다 — 컨테이너 안에서
      `build_registry().list_tools()` 를 실제로 읽었다(소스 grep 아님).
      `trigger` 스키마가 `additionalProperties: false`
- [ ] 🚫 **PWA 는 못 쟀다.** Vercel Production 배포는 `92392bd` 로 success
      (08:58:15Z)지만, 번들에서 `Trigger on` 이 사라졌는지는 **확인 못 했다**:
      로그인 페이지가 부르는 청크 11개를 받아 뒤졌더니 죽은 문자열 0건인데
      **양성 대조군 `Output mode` 도 0건**이었다 ⇒ 그 표면이 애초에 이 청크에
      없다. **0 이 부재의 증거가 아니다.** 제품 상세는 인증 뒤라 열려면
      SSO 가 붙은 Playwright(스킬 `playwright-sso-auth-e2e`)가 필요하다.
      간접 증거만 있다: 그 트리에서 `tsc` · `vitest`(802) · `next build` 통과

## C. 안 건드린 것 (의도)

- **`trigger.filters` 의 UI 는 여전히 없다.** 체크박스를 지우면서 대체 컨트롤을 넣지
  않았다 — "반응하나"는 `filters` 가 말하고, 그건 커넥터마다 모양이 달라 한 UI 로
  안 덮인다(B10c). 형님이 on/off 를 원하시면 그건 **되살리기가 아니라 새 기능**이다
- **`output_mode`** — 이 표면이 실제로 쓰는 노브. 그대로 둔다
- **09-18 행을 만든 주체를 특정하지 않았다.** `audit_outbox` 에 `bsvibe.mcp.*` 이벤트가
  **한 건도 없다**(5,571행 전부 execution/ontology) ⇒ 감사로는 못 가린다. 소거법으로
  MCP 가 유일한 열린 문이었다: PWA 는 `trigger`·`selection` 을 안 보내고(그 행은 `selection`
  이 채워져 있다), REST 는 `extra=forbid` 로 거절한다

## D. 절단 실증 (가드가 진짜 잡는가)

| 끊은 전선 | 컴파일 | 뒤집힌 칸 |
|---|---|---|
| MCP `trigger` 를 `dict[str, Any]` 로 되돌림 | ✅ | **5** (스키마 4 + 디스패처 1) |
| 툴 **설명만** 옛 문장으로 | ✅ | **2** (와이어 설명 · 소스 텍스트) — 스키마는 초록 유지 |
| 마이그레이션 기본값만 옛 모양으로 | ✅ | **1** (라이브 DDL) |
| PWA 체크박스 복구 | ✅ | **1** |

⇒ 네 층이 **각각 독립으로** 감시된다. 설명만 되돌렸을 때 스키마 테스트가 초록이었던 것이
그 증거다 — 한 가드가 다른 층을 대신 증명하지 않는다.
