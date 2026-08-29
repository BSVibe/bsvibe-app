# 추월당한 두 표면 삭제 — 검증 체크리스트

> 2026-08-29. `~/Docs/BSVibe_Reality_Audit_2026-07-14.md` 의 **5-4** · **5-12**.
> 그 문서는 STALE 배너를 달고 있어 **전수 재측정 후에** 착수했다.

## 재측정이 먼저였다 — 그리고 절반이 이미 해소돼 있었다

| 감사 항목 | 감사 (2026-07-14) | 재측정 (2026-08-29) |
|---|---|---|
| 5-11 `SafeModeBoundary.gate` | 삭제 필요 | ✅ **이미 해소** — 심볼 0 hits |
| MCP parity 5갭 | 5갭 | 🟡 **대부분 해소** — checkpoints · safe_mode(7) · `runs_show` 존재. 남은 것: `runs/retry` · `deliverables/retract` · `diff` · `report` |
| **5-4 `VerifierWorker`** | 런타임 미등록 | ❌ 여전 → **이 PR** |
| **5-12 벌크 만료** | 호출자 0 | ❌ 여전 → **이 PR** |
| 5-9 `expire_stale` | 호출자 0 | ❌ 여전 (처방은 삭제가 아니라 **배선** — 별건) |
| 브라우저 E2E · CLI 제품 표면 | 0 | ❌ 여전히 0 |
| B0 샌드박스 egress | `--network none` 없음 | ❌ **여전히 열림** — 형님 판단 대기 |

⇒ 08-28 세션이 77건 중 44건(57%)이 이미 해소된 걸 발견했는데, 남은 것에서도
같은 일이 반복됐다. **감사 문서를 근거로 바로 착수했으면 두 항목을 헛짚었다.**

## 지운 것과 그 근거

**5-4 `VerifierWorker`** — 프로덕션 import **0**, 런타임 워커 목록에 없음
(`IntakeWorker`·`AgentWorker`·`DeliveryWorker`·`NotifyWorker`·`DailyBriefWorker`·
`AuthDependencyWorker`·`SettleWorker`·`RelayWorker`·`ScheduleWorker`×4 뿐).
살아 있는 검증 경로는 `verification_service` 다. 등록이 답이 아닌 이유는 감사가
적었다 — 인라인 처리와 **claim 경합**이 생긴다.

**5-12 벌크 만료** (`SafeModeQueue.expire` · `repo.mark_expired_bulk`) —
프로덕션 호출자 **0**. 살아 있는 sweep 은 `SafeModeExpirySweepRunner` 이고
그 파일이 스스로 적어 뒀다: *"Goes through `SafeModeQueue.mark_expired`
**per row** (NOT a bulk ...)"*.

**둘 다 테스트만이 살려 두고 있었다** — 전제가 거짓이 된 뒤에도 동작을 고정하는
알리바이. 이 저장소에서 반복해 나온 모양이다.

**덤**: `safe_mode_queue.py` docstring 이 `expire_all_due` 를 가리키는데
**그런 메서드는 트리 전체에 정의가 0**이었다. 죽은 표면을 지우며 그것을
설명하던 산문도 같이 지웠다.

## ⭐ 커버리지는 하나도 안 버렸다 — 살아 있는 축으로 이설했다

| 원래 | 어디로 | 왜 |
|---|---|---|
| `test_expire_sweeps_overdue` (벌크) | `list_due_expired` + per-row `mark_expired` | `SafeModeExpirySweepRunner` 가 실제로 도는 경로 |
| `test_mark_expired_bulk_workspace_scoped` (리포 레벨) | 서비스 레벨 `test_mark_expired_is_workspace_scoped` | **테넌트 격리는 지울 수 없다.** per-row 경로가 `row.workspace_id != workspace_id → False` 로 같은 성질을 강제하는 것을 코드로 확인하고 옮겼다 |

이설한 격리 테스트에는 **양성 대조군**을 함께 넣었다 — 주인은 뒤집을 수 있다는
단언이 없으면 "언제나 False" 인 구현도 통과한다.

## 체크리스트

- [x] `verifier_worker.py` 파일과 `VerifierWorker`/`VerifierAdapter`/`VerifierConfig`
      식별자가 트리 어디에도 없다 (AST 스캔)
- [x] `mark_expired_bulk` 가 트리 어디에도 없다
- [x] `expire_all_due` 를 가리키는 산문이 없다 (이건 텍스트로 센다 — 사라져야
      하는 것이 산문 자체이므로)
- [x] **음성 대조군 4경로 — 각각 자기 테스트만 떨어뜨린다**
      (파일 재도입 · 다른 파일에서 식별자 참조 · 벌크 재도입 · 유령 산문 부활)
- [x] 양성 대조군: 스캐너가 살아남는 워커(`DeliveryWorker`)를 실제로 찾는다
      — 스캐너가 고장 나 있으면 부재 주장이 공짜로 참이 된다
- [x] 양성 대조군: 살아 있는 검증 경로(`VerificationService.verify`)가 그대로
- [x] 양성 대조군: 살아 있는 만료가 여전히 per-row (삭제의 **근거** 자체를 명제로)
- [x] 양성 대조군: 등록된 워커 목록이 이 삭제로 줄지 않았다
- [x] 전체 게이트 (fresh PG · pytest+cov80 · ruff · format · mypy · lint-imports)

## ⭐ 사라진 테스트를 **전수 대조**했다 — 총계는 거짓말을 한다

브랜치와 main 이 **둘 다 6495건**으로 같았다. 산술로는 +1 이어야 해서 어긋났는데,
총계만 보면 "변화 없음"으로 넘어갈 자리다. 삭제 PR 에서 조용히 사라진 테스트는
정확히 여기 숨는다.

수집된 **테스트 ID 목록을 통째로 diff** 하니 **15건 사라지고 15건 생겼고**, 전부
설명됐다:

* verifier 4건 (죽은 표면) · 벌크 리포 1건 + `expire_sweeps_overdue` 1건 (이설)
* parametrize 3건 — shim 2건 + `tests/glue/test_imports.py` 1건.
  **마지막 것이 내 산술이 놓친 것이다**: 그 파일은 모듈을 자동 열거하므로
  모듈을 지우면 테스트가 하나 줄어든다. 내가 손댄 파일 목록에 없었다
* delivery 6건 — 파일 rename (`test_delivery_verifier_workers.py` →
  `test_delivery_worker.py`)

⇒ **개수 비교는 삭제를 검증하지 못한다. ID 집합을 비교하라.**

## 남는 것

* **5-9 `expire_stale`** — 감사의 처방은 삭제가 아니라 **배선**(완전 구현 +
  유닛테스트 10개, 호출자만 0). 별 PR.
* **B0 샌드박스 egress** — 감사가 처방한 `--network none` 을 그대로 넣으면
  `uv sync`·`git fetch` 가 막혀 **모든 실런이 죽는다**. 처방이 있는 것처럼
  보였지만 실제로는 없었고, 그게 이 항목이 살아남은 이유로 보인다. 형님 판단 대기.
