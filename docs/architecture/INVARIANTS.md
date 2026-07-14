# BSVibe — Architecture Invariants

**이 문서는 레포 안에 있다. 그것이 요점이다.**

2026-07-14 전수 감사에서 ~35건의 결함이 발견됐고, 전부 같은 뿌리였다: **아키텍처의 진짜 엣지는 문자열(테이블 이름·스트림 키·이벤트 prefix·`artifact_type`·커넥터 이름)인데, 어떤 도구도 그것을 볼 수 없다.** 계층 분리·bounded context·Protocol·`lint-imports`·`mypy --strict`·80% 커버리지가 전부 있었고, **35건을 하나도 못 막았다.**

그중 결정적 요인 하나: **아키텍처 문서가 레포 밖(`~/Docs/`)에 있었다.** 리뷰어는 PR에 없는 문서로 PR을 검증할 수 없다. 그래서 이 문서가 여기 있다. **불변식은 코드 옆에 산다. 핸드오프·상태·감사 기록은 `~/Docs/`에 남는다.**

---

## 진단: 왜 모든 방어선이 실패했나

**표본**: `worker_runtime.py`에서 **같은 `ScheduleWorker` 클래스가 같은 Protocol로 3번** 생성된다.
- `SafeModeExpirySweepRunner` → 살아있음
- `AuditRetentionSweepRunner` → 살아있음
- `DbPollScheduleRunner` → **죽었음** (`workspace_schedules`에 INSERT하는 프로덕션 경로가 0개)

**차이가 어떤 모듈·타입·import 엣지·테스트에도 없다.** 차이는 "다른 서브시스템이 그 테이블에 INSERT를 하느냐"뿐이다. **결함이 코드 안에 없으므로, 코드를 검사하는 도구는 못 찾는다.**

| 방어선 | 왜 실패했나 |
|---|---|
| `lint-imports` | 계약 5개가 **전부 `forbidden` 타입**. 금지는 *존재하는* 엣지에 발동하고, 미배선은 *없는* 엣지다. 게다가 producer→consumer 커플링이 **import가 아니라 테이블 이름**이라 import 그래프는 완벽히 건강해 보인다 |
| `mypy --strict` | 반쪽들을 타입체크한다. 호출 여부는 개념이 없다. **더 나쁘게, `X \| None`이 fail-open 버그를 만들었다** — `None`이 "적용 불가"와 "도출기가 터졌다"를 동시에 의미하는데, `is not None` 분기는 교과서적으로 옳아서 **mypy가 그것을 인증한다** |
| Protocol / ABC | **seam은 초대장이지 의무가 아니다.** `NotificationInterface` Protocol은 구현체 0개인데 필드가 `\| None` 기본값 `None`이라 가드가 영원히 false. 예외도 안 난다 |
| 80% 커버리지 | **이 결함 클래스에서는 정확성과 음의 상관.** 테스트가 `WorkspaceScheduleRow(...)`를 생성한다 — **테스트 스위트가 그 테이블의 유일한 producer다.** 고아 consumer를 성실히 테스트할수록 더 green해진다 |
| 코드 리뷰 | **모든 PR이 개별적으로 옳았다.** 리뷰어는 "이 변경이 좋은가"를 평가했고 답은 예였다. **"나머지 절반이 존재하는가"는 레포의 어떤 산출물도 말해주지 않았다** |

---

## 불변식

### INV-1. 채널은 선언된 객체다 (Channel Registry)

**producer/consumer 커플링을 문자열에서 타입 객체로 승격한다.**

```python
# backend/channels.py
WORKSPACE_SCHEDULES = Channel(
    row=WorkspaceScheduleRow,
    producers=[...],            # 비어 있으면 머지 불가
    consumers=[...],            # 비어 있으면 머지 불가
    authoring_surface="POST /api/v1/schedules",   # 사람/에이전트가 만드는 행이면 필수
)
```

- 그 행을 쓰는 **유일한 합법 경로**는 `CHANNEL.emit(producer_id, ...)`
- 그 채널을 읽는 **유일한 합법 경로**는 `CHANNEL.consume(consumer_id)`
- 메타테스트: `for ch in ALL_CHANNELS: assert ch.producers and ch.consumers` (+ 사람 기원 채널은 `authoring_surface` 필수)
- 강제: bare `session.add(XRow(...))` 금지 grep 테스트 (레포에 이미 grep 메타테스트 관용구가 있다 — `test_dunder_all_coverage.py` 등)

**적용 범위 (파운더 결정, 2026-07-14 — 최대 범위):**
1. **워커가 소비하는 DB 큐/테이블** — `trigger_events`, `requests`, `delivery_events`, `execution_run_activities`, `audit_outbox`, `safe_mode_queue_items`, `workspace_schedules`, notification outbox
2. **이벤트버스 prefix + Redis 스트림 키** — `InProcessEventBus.publish`가 임의 문자열을 받는 것과, **구독자 예외를 삼키는 것**(`bus.py:49`)을 함께 해결한다. *구독자가 없는 것*과 *구독자가 매번 던지는 것*이 런타임에서 구분 불가능한 현 상태는 그 자체로 결함이다
3. **커넥터 / `artifact_type` 레지스트리** — 현재 커넥터 정체성이 **3곳**에 중복 선언돼 있다: 플러그인 데코레이터(`@p.outbound`) / `backend/connectors/kinds.py`(하드코딩 맵) / `apps/pwa/lib/api/types.ts`(*"Mirror of backend.connectors.kinds"*). **SoT는 `PluginMeta` 하나다.** `kinds.py`의 하드코딩 맵과 PWA 미러를 **삭제**한다. (`kinds.py:12`의 *"we intentionally do NOT derive it from the plugin registry"* 는 폐기된 결정이다)

> 이것이 `linear`/`trello`가 완성돼 있는데 UI에서 못 만들고, `sentry` outbound가 "Connected"를 표시하면서 아무것도 전달하지 않고, `sentry_issue_update`/`issue_comment`가 `ArtifactType` Literal에 없어 도달 불가인 이유다. **레지스트리 단일화로 이 세 결함이 동시에, 구조적으로 사라진다.**

### INV-2. 안전 경로는 fail-closed다 (3-state, not `| None`)

**`None`이 "N/A"와 "체크가 실패했다"를 동시에 의미하면 안 된다.**

```python
Gate = Ok(...) | NotApplicable(reason) | Failed(reason)
```

- **`Failed`는 fail-closed.** 검증 게이트를 *돌리지 못한 것*은 통과가 아니다.
- **"정말 없는 것"과 "할 게 있는데 못 한 것"의 구분은 결정론적이어야 한다** (파운더 결정, 2026-07-14). manifest/툴체인 탐지가 `applicable`을 정한다. **LLM이 `applicable: false`를 자기 선언하는 것은 신뢰하지 않는다.**
- **`all([])`가 게이트를 만족시키면 안 된다.** 커맨드 0개 = "게이트가 돌지 않음" ≠ "게이트 통과".

레포 선례: `retraction_service.py:66` `UndoResult = Literal["undone","expired","already_applied",...]`. 이 규율을 복사한다.

**적용 범위**: 전면 마이그레이션 금지. `None`이 실패와 부재를 겸하는 **~12곳만** (`verification_service.py`의 `except Exception: return None` 4곳, `InProcessEventBus.publish`의 예외 삼킴 등).

### INV-3. 프로덕션 티어 테스트 (`tests/production/`)

**기존 유닛/글루 테스트는 그대로 둔다.** 별도 티어를 신설한다:

- 진짜 `create_app()` + 진짜 `build_worker_runtime()`
- conftest가 **`app.dependency_overrides == {}` 를 단언**한다
- **`*Row` 직접 생성 금지.** 모든 행은 진짜 라우트 / MCP 툴 / 워커 틱을 구동해서 도착해야 한다
- 서브시스템당 최소 1개: *"authoring 표면을 구동 → 워커 틱 → 관측 가능한 효과 단언"*
- 이 티어의 픽스처는 **커버리지에서 제외**한다 (게이트가 자기 시드로 만족되는 것을 막는다)

> **이 티어에서 테스트를 *쓸 수 없다*는 사실 자체가 버그 리포트다.** cron은 구동할 authoring 표면이 없어서 이 테스트를 애초에 작성할 수 없다.

**RLS/테넌시**: BSVibe는 **멀티테넌트 SaaS(공개 가입)** 다 (파운더 확인, 2026-07-14). 현재 `get_workspace_id` override 때문에 **`set_workspace_guc()`가 테스트에서 한 번도 실행되지 않는다** → **Postgres RLS 테넌트 격리가 API를 통해 검증된 적이 0회.** 이 티어의 **첫 번째 테스트**가 테넌트 격리여야 한다. **출시 차단 항목.**

### INV-4. 하나의 계약, 생성된 클라이언트

**PWA·MCP·CLI 3중 parity를 유지하되, 손으로 미러링하지 않는다** (파운더 결정, 2026-07-14).

- 51개 REST 라우트 전부에 **Pydantic `response_model=` + 명시적 `operation_id`** (현재 각각 5개/3개뿐 — 지금 codegen하면 ~25%가 `unknown`으로 생성된다)
- OpenAPI → TS 타입(PWA) + Python 클라이언트(CLI) **생성**
- `apps/pwa/lib/api/types.ts`의 수작업 1,541줄과 CLI의 손수 짠 호출을 **삭제**한다
- **동적 집합(채널·커넥터·모델·artifact_type)은 하드코딩 배열이 아니라 API에서 온다** (INV-1의 귀결)

### INV-5. Lift는 채널 단위로 자른다

**"한 번에 한 lift"는 유지하되, producer/consumer 이음매를 따라 자르지 않는다.**

이번 결함의 상당수는 Lift 슬라이싱이 **각 lift를 개별적으로 완결돼 보이게** 만들었기 때문에 생겼다 (Lift A~R2c의 흔적이 코드 곳곳에 있다). M3a가 tombstone 경로를 배선하고 M3b(실제 field-rewrite)가 오지 않아 **"수정" 버튼이 no-op인 채 감사 로그에는 "적용됨"을 남기는** 것이 대표 사례다.

> **하나의 lift는 최소 하나의 채널을 end-to-end로 배달한다: authoring 표면 → producer → 채널 → consumer → 관측 가능한 효과.**
> 절반만 배달하는 lift는 머지하지 않는다. INV-1의 `producers=[]` 금지가 이것을 기계적으로 강제한다.

### INV-6. 기술 스택

- **데이터: Postgres + NetworkX** (파운더 확인, 2026-07-14). `backend/knowledge/graph/graph_store.py`의 **SQLite `GraphStore`는 스택 외**다 — 삭제 대상이며, 그래프 영속화는 Postgres + NetworkX로 **재설계**한다.
- Python 3.11+ / uv / pydantic-settings / structlog / async I/O — 기존 규율 유지.

### INV-7. 툴 계약은 하나다 — chat ≡ executor (파생, 절대 손수 미러링 금지)

**제1원칙: chat과 executor는 동등하다.** 파운더가 개발 내내 겪은 가장 큰 고통이 여기서 나왔다 ("chat만 되고 executor는 안 된다").

**물리적 제약**: **파일은 서버에 있다** (`run_worktree_path(run_id)`). **executor CLI는 사용자 로컬 워커에서 돈다.** 따라서 **executor는 자기 자신의 빌트인 툴(Read/Write/Bash)을 절대 쓰면 안 된다** — 그것은 틀린 파일시스템을 본다. BSVibe가 등록한 **remote MCP 툴만** 쓴다.

**현재 결함 (2026-07-14 감사)**: 툴 계약이 **3개 목록 + 2개 레지스트리**에 중복돼 있고, 일치를 강제하는 것이 없다.

| | ① `WORK_TOOL_NAMES`<br>(CLI에 광고) | ② MCP transport 레지스트리<br>(`build_run_tool_registry`) | ③ in-process 레지스트리<br>(`_drive_loop` + `register_knowledge_tools`) |
|---|---|---|---|
| `knowledge_search` | ✅ | **❌ `Unknown tool`** | ✅ |
| `invoke_skill` | **❌ 광고조차 안 함** | **❌** | ✅ |
| `<connector>__<action>` | **❌** | **❌** | ✅ |
| `file_edit` | ✅ | **❌ 인자명 불일치로 100% 실패** | ✅ |

**불변식:**
1. **툴 레지스트리 빌더는 하나다.** `build_run_tool_registry`(MCP transport)와 `_drive_loop`(in-process)는 **같은 팩토리**를 호출해야 한다. 둘이 각자 `ToolRegistry(...)`를 짓는 현 구조는 금지.
2. **`WORK_TOOL_NAMES`는 손으로 관리하지 않는다 — 레지스트리에서 파생한다.** 광고한 툴이 실제로 없으면(또는 그 반대면) 그 자체가 빌드 실패여야 한다.
3. **상태는 런에 산다, 레지스트리에 살지 않는다.** `declared_contract` / `declared_knowledge` / `grounded_paths` / `written_paths` — MCP transport는 요청마다 새 레지스트리를 짓기 때문에, 루프가 그것을 못 보면 *"작업은 다 하고 커밋까지 했는데 검증 선언을 안 했다"* 는 거짓 결론이 난다 (실제로 발생 중).
4. **샌드박스 매니저는 싱글턴이다.** `build_sandbox_manager()`를 요청마다 부르면 컨테이너 캐시가 비어 있어서 **매 툴 호출이 `docker rm -f` 후 재생성**한다 — 300초 타임아웃의 정체이자, 같은 제품의 병렬 런을 죽이는 원인.
5. **executor 시스템 프롬프트는 T2 현실을 말해야 한다.** 현행 `_E30_TOOL_GUIDE_HEADER`는 *"Use your OWN tools — Read/Edit/Write/Bash … BSVibe does NOT call tools on your behalf"* 라고 **정반대를 지시**하고, 참조 툴 이름도 접두사가 없어 하나도 안 맞는다.
6. **chat 턴은 모든 executor에서 툴이 꺼진다.** `claude_code`만 고쳐졌고 `codex`/`opencode`는 `agentic` 플래그를 읽지도 않는다 — chat 모양 호출자(frame/judge/ingest)가 그쪽으로 가면 여전히 agentic CLI가 뜬다.
7. **워커 로컬 클론 + scrape-back 경로는 삭제한다.** 굶어 있을 뿐 장전돼 있고, `LocalFilesystemArtifactStore`의 루트가 **서버 런 워크트리와 같은 디렉터리**라 발동 시 truncated 파일을 0바이트로 서버에 덮어쓴다.

**테스트 규율**: MCP 툴 delegation 테스트의 fake registry를 **진짜 `ToolRegistry`로 교체**한다. 지금은 인자를 기록만 하고 핸들러를 안 불러서 `file_edit`의 인자명 불일치가 통과했다.

### INV-8. Safe Mode는 아웃바운드만 게이트한다

파운더 확인(2026-07-14): **Safe Mode는 "세상으로 나가는 것"만 막는다.** 내부 지식 정규화(`create-concept` auto-apply)는 genuine risk가 아니므로 자동 적용이 의도대로다.

**따라서 파운더 알림은 Safe Mode를 우회한다** — 알림을 deliverable 경로에 태우면 *알림을 승인하려면 알림을 받아야 하는* 데드락이 생긴다.

---

## 명시적으로 기각된 것

**정적 도달가능성 게이트 (vulture / pycg 류) — 기각.**
플러그인·웹훅 파서·커넥터 디스패치가 전부 **런타임 문자열 조회**로 해결된다. 정적 도구는 `plugin/*` 전체를, 모든 SQLAlchemy 모델을, 모든 Pydantic 응답모델을, 모든 Protocol을 죽었다고 본다. 첫날부터 예외 목록이 수백 개가 되고, 이 레포는 이미 그 말로를 보여준다(한 계약에 `ignore_imports` 25개).

**그리고 결정적으로, 대표 결함에 clean bill of health를 줬을 것이다** — `ScheduleWorker`는 도달 가능하다(매 틱 `run()`이 await된다). **도달가능성은 틀린 질문이다.** 질문은 "그 *테이블*에 writer가 있는가"다.

---

## 한 문장

> **채널을 "선언하지 않으면 읽을 수도 쓸 수도 없는 타입 객체"로 만들면, 고아-절반 결함이 불가능해진다.** 린트 규칙을 위에 덧대는 것이 아니라.

---

*감사 원본: `~/Docs/BSVibe_Reality_Audit_2026-07-14.md` · 진단 스킬: `half-wired-subsystem-audit`*
