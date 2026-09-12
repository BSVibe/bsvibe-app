# BSVibe Worktree-Based Workspace Design

**Date**: 2026-05-27
**Status**: Spec — 구현 전
**Author**: founder + assistant

## 0. 동기 (Reality Audit)

`e2e-hello` 프로덕트 검사에서 노출된 구조적 문제:

1. **"Shipped" 의미 미정의** — `RunStatus.SHIPPED`가 실제로 어디에도 머지하지 않음. 단순 플래그.
2. **Product Files 뷰 무의미** — verified run의 deliverable이 평면 리스트로 나열되어 동일 파일명 둘이 구분 없이 공존.
3. **Per-run sandbox가 product와 단절** — 새 run은 항상 빈 dir 또는 github clone에서 시작.
4. **Failed run의 이유 미surface** — "Didn't finish. Nothing for you to do" 한 줄.

근본 원인: product의 *canonical state*라는 모델이 없음.

## 1. 핵심 원칙

> **머지는 작업한 agent의 책임. founder는 *semantic intent*만 본다.**

- 작업한 agent가 자기 worktree 안에서 충돌까지 해결 (Claude Code식)
- 새 run/call 안 spawn
- founder는 raw `<<<<<<<` 마커 안 봄. 의미 모호한 케이스만 `ask_user_question`으로 surface (기존 Decision 메커니즘 재사용)
- Safe Mode는 **외부 배포** 게이트지 머지 게이트가 아님

## 2. 모델

### 멘탈 모델

```
┌─ Product (e2e-hello) ─────────────────────────────────────────┐
│   var/products/<product_id>/        ← git repo (canonical)    │
│      hello.py                                                  │
│      .bsvibe/PRODUCT.md                                        │
│                                                                │
│   branches:                                                    │
│      main                       ← shipped state                │
│      bsvibe/run/<run_a_id>      ← Run A's branch               │
│      bsvibe/run/<run_b_id>      ← Run B's branch               │
│                                                                │
│   worktrees:                                                   │
│      var/runs/<run_a_id>/ → bsvibe/run/<run_a_id>             │
│      var/runs/<run_b_id>/ → bsvibe/run/<run_b_id>             │
└────────────────────────────────────────────────────────────────┘
```

### 라이프사이클

```
Product create → git init main + .bsvibe/PRODUCT.md initial commit

Run start (workspace_provisioner) → git worktree add var/runs/<rid> -b bsvibe/run/<rid> main

Agent loop:
  iterations (file_write/edit, shell_exec, ...)
  → agent declares verification
  → backend runs verify:
      step 1: command checks (기존)
      step 2: git -C <worktree> merge main           ← NEW
      step 3: clean merge + commands pass → verified
              OR conflict → FAIL with reason="merge_conflict" + paths
  → on FAIL: agent gets next round (기존 mechanism)
              worktree has conflict markers
              agent uses file_read/edit/shell_exec(git log/diff) to resolve
              ask_user_question if semantic intent unclear
              → re-declare verify
  → on PASS: AgentRunner.transition(REVIEW_READY)
              backend: git -C <main> merge --ff-only <branch>    ← advisory lock
              → SHIPPED + worktree+branch cleanup

Executor run (claude_code/codex/opencode, single-shot, no retry):
  Same up to verify. On conflict: verification_failed Decision (기존 path).
  founder ship_anyway action → backend: git merge -X theirs <branch> (run wins).
  founder discard action → cancel + cleanup (기존).
```

### Verify 의 두 단계 — 하나로 통합

기존 verify 는 command checks + judge checks. 여기에 **merge check 를 가장 앞에**
추가:

```
verify(work_step, contract, box, ...):
    1. if product workspace bound:
         git -C <worktree> merge main
         if conflict: return VerificationResult(FAILED, reason="merge_conflict", 
                                                 conflict_paths=[...])
    2. command checks (기존)
    3. judge checks (기존)
    4. all pass → PASS
```

Merge check 가 fail 하면 command/judge 는 안 돌림 (의미 없음). 그 fail 자체가
agent 에게 "merge 충돌 발생" 신호.

### founder 가 보는 것

| 케이스 | UI |
|---|---|
| Native run 이 clean verify → clean ship | 아무 Decision 없음. Run 이 SHIPPED 로 표시. |
| Native run 이 merge conflict, agent 해결 가능 | Agent loop 안에서 처리. founder 안 봄. Run 이 SHIPPED 로 표시. |
| Native run 이 merge conflict, agent 가 의미 모호함을 만나 ask_user_question | 기존 Decision UI (semantic question + options + Other). founder 답변 → agent resume → ... → SHIPPED. |
| Executor run 이 clean verify → clean ship | 동일 |
| Executor run 이 merge conflict | `verification_failed` Decision (기존 L-D2 UI: "Approve & ship" / "Discard"). `Approve & ship` = `-X theirs` merge. |
| Run 이 round-cap 도달, 해결 못 함 (rare) | Run 이 FAILED + reason="round_cap" + W3 UI 에서 reason surface |

## 3. PR 분해

### PR W1: 워크스페이스 + worktree + 빈 verify (merge 없음, dogfood 용)

**목표**: git repo 와 worktree 가 동작. verified run 은 아직 flag-only SHIPPED.
dogfood 로 FS 레이아웃 검증.

**Files**:
- `backend/storage/product_workspace.py` (신규):
  - `product_workspace_path(product_id) -> Path`
  - `init_product_workspace(product_id)` (idempotent)
  - `add_run_worktree(product_id, run_id) -> Path`
  - `remove_run_worktree(product_id, run_id, *, delete_branch=True)`
- `backend/api/v1/products.py` — create 시 `init_product_workspace`
- `backend/api/main.py` — startup hook: 기존 ProductRow lazy init
- `backend/workers/run.py` — `build_workspace_provisioner` 분기 (github > product > legacy)
- Advisory lock helper (BSNexus `~/Works/BSNexus/main/backend/src/core/advisory_lock.py` 에서 lift)
- `backend/api/v1/checkpoints.py` — `ship_or_discard` synth 제거 (의미 없어짐)
- One-shot SQL: e2e-hello cleanup (runs/deliverables/decisions/trigger_events DELETE)

**Tests**: worktree round-trip, idempotent init, github provisioner 우선순위,
advisory lock race, e2e-hello reset 후 새 run 이 worktree 에서 시작.

**Behavior**: verified → 여전히 flag-only SHIPPED (merge 안 함). 다음 PR 에서
머지 추가.

### PR W2: Verify-time merge + native auto-resolution + executor ship-anyway

**목표**: verify 에 `git merge main` 추가. Native run conflict 시 agent retry,
executor conflict 시 verification_failed Decision.

**Files**:
- `backend/execution/verifier/service.py` (또는 sibling) — verify 진입점 앞에 merge check 추가
- `backend/orchestrator/agent_runner.py` — `transition(REVIEW_READY)` 시 main 으로 `git merge --ff-only <branch>` (advisory lock + cleanup)
- `backend/api/v1/checkpoints.py` — `_ship_decision_run` (executor 경로) 에 `-X theirs` merge strategy 추가
- 시스템 프롬프트 보강 — agent 에게 "verify 가 merge conflict 로 실패하면 worktree 에 마커가 있다. 해결하고 재시도해라"
- `backend/execution/verifier/contract.py` — VerificationResult 에 `conflict_paths` 필드 추가

**Tests**: 
- native clean merge → SHIPPED
- native conflict → agent retry → resolved → SHIPPED
- native conflict → agent ask_user_question → Decision → founder 답변 → resolved → SHIPPED
- executor clean merge → SHIPPED
- executor conflict → verification_failed Decision → founder ship_anyway → `-X theirs` merge → SHIPPED
- executor conflict → founder discard → CANCELLED
- advisory lock: 동시 두 run ship → 직렬화, 두 번째는 main 이동 후 다시 시도

### PR W3: ProductFiles 단일 트리 + Run 실패 detail

**Files**:
- 신규 엔드포인트:
  - `GET /api/v1/products/{id}/files` → `git ls-files` 트리
  - `GET /api/v1/products/{id}/files/{path:path}` → content + last commit metadata
- PWA `ProductFiles.tsx` 재작성 — per-deliverable 그룹 제거, 한 트리, 각 파일에 last-touched-by-run 라벨
- PWA `RunDetail.tsx` — failed/cancelled 시 history.reason + activities[error] surface

## 4. API 상세

### `backend/storage/product_workspace.py`

```python
async def init_product_workspace(product_id: uuid.UUID) -> None:
    """git init, .bsvibe/PRODUCT.md commit on main. Idempotent."""

async def add_run_worktree(product_id, run_id) -> Path:
    """git worktree add var/runs/<rid> -b bsvibe/run/<rid> main. Returns worktree path."""

async def remove_run_worktree(product_id, run_id, *, delete_branch=True) -> None:
    """git worktree remove + git branch -D. Idempotent (no-op if missing)."""

async def merge_to_main(product_id, run_id) -> str:
    """git -C <main> merge --ff-only <branch>. Returns commit SHA. 
       Fails if not fast-forward (= someone else shipped between agent's 
       merge-main and this call — caller retries with re-verify)."""

async def merge_main_into_worktree(product_id, run_id) -> MergeResult:
    """git -C <worktree> merge main. Returns MergeResult(status, conflict_paths).
       Leaves worktree in merge state on conflict (no abort)."""

async def force_merge_theirs(product_id, run_id) -> str:
    """git -C <main> merge -X theirs <branch>. For executor ship_anyway path."""

class MergeResult(BaseModel):
    status: Literal["clean", "conflict", "stale"]
    conflict_paths: list[str] = []
```

### Verify 통합

```python
# backend/execution/verifier/service.py
async def verify(*, run, work_step, attempt, contract, box, written_paths, final_text):
    # NEW: merge check first
    if run.product_id is not None:
        merge_result = await merge_main_into_worktree(run.product_id, run.id)
        if merge_result.status == "conflict":
            return VerificationResult(
                outcome=VerificationOutcome.FAILED,
                reason="merge_conflict",
                conflict_paths=merge_result.conflict_paths,
                ...
            )
    # ... existing command checks + judge checks
```

Native agent loop 은 verify FAIL 을 기존 mechanism 으로 받음. failure reason 이
`merge_conflict` 면 시스템이 다음 round LLM 메시지에 명시 안내 추가:
"Merge conflict in your worktree at: hello.py, README.md. Resolve and re-verify."

### AgentRunner.transition

```python
async def transition(self, *, run_id, to_status, reason=None):
    # ... existing code ...
    
    if to_status is RunStatus.REVIEW_READY and run.product_id is not None:
        if not is_executor_run(run):
            # Native: ship inline (we just verified, including merge into worktree)
            async with product_advisory_lock(session, run.product_id):
                try:
                    sha = await merge_to_main(run.product_id, run_id)
                except StaleMergeError:
                    # main moved during our verify; revert to RUNNING for one more round
                    run.status = RunStatus.RUNNING
                    return False
                run.status = RunStatus.SHIPPED
                await remove_run_worktree(run.product_id, run_id)
        else:
            # Executor: synthesize ship_or_discard or keep at REVIEW_READY for founder
            # (verification_failed Decision handles the conflict case via its own L-D2 action)
            run.status = RunStatus.REVIEW_READY  # keep as-is, executor mints Decision
```

(실제로 executor 의 verified-path 는 별도 분기 — 추후 확인.)

## 5. 실패 모드

| 시나리오 | 동작 |
|---|---|
| Product workspace .git 손상 | `add_run_worktree` raises → run.terminal_reason="product_workspace_corrupt" |
| Verify-time `git merge main` 충돌 (native) | Verify FAIL with reason="merge_conflict". Agent retry. |
| Native agent round-cap (해결 못 함) | `terminal_reason="round_cap"` (기존). W3 UI 에서 reason + 메시지 surface |
| Native agent 가 ask_user_question 호출 | 기존 Decision UI |
| Verify-time `git merge main` 충돌 (executor) | `verification_failed` Decision (기존). founder ship_anyway → `-X theirs` |
| Executor ship_anyway 후에도 conflict 못 풀리는 경우 (rare) | -X theirs 는 자동 해결을 강제하므로 실패 케이스 없음. 단 binary file 등은 별도 처리 (v1 미지원) |
| Ship 시 fast-forward 불가 (main 이동) | StaleMergeError → run 이 RUNNING 으로 복귀, verify 재실행 (agent 모름) |
| 두 run 동시 ship | advisory lock 으로 직렬화 |
| 워크트리 cleanup 실패 (open handle 등) | 다음 worker tick 에 재시도 (idempotent). Ship 자체는 완료. |

## 6. 보안

- 모든 git CLI: `asyncio.create_subprocess_exec` argv 형식 (no shell=True)
- run_id / product_id: UUID 검증 후 인자로
- 워크트리 path boundary: 기존 sandbox manager mount 경계
- `/files` 엔드포인트: Postgres RLS workspace-scoped

## 7. Out of scope (v1)

- PWA 내 conflict editor (founder 가 마커 직접 편집) — VS Code extension future
- Multi-merge dependency UI — git 자연 처리, UI 없음
- `git gc` 자동화 — 디스크 압력 관찰 후
- Binary file 충돌의 Executor `-X theirs` 한계 — v1 미지원
- Late-stage rollback UI (SHIPPED 이후) — `git revert` 가능하나 UI 없음

## 8. Open decisions (구현 중 확정)

- **Commit author**: `BSVibe Agent <agent@bsvibe.dev>` 하드코드 v1
- **Commit message**:
  - Worktree commit (agent 의 마지막 commit): `"<intent[:50]> (run-<short>)"`
  - Ship 의 fast-forward 는 commit message 그대로 가져감
  - Force-theirs merge: `"ship: <intent> (run-<short>) [ship_anyway]"`
- **Branch prefix**: `bsvibe/run/`
- **Initial commit content**: `.bsvibe/PRODUCT.md` (id/name/slug/created_at)
- **Verifier 가 merge attempt 전 agent 에게 commit 강제 여부**: 첫 verify 전 worktree 에 uncommitted 변경이 있으면 backend 가 자동 `git add -A && git commit -m "..."` 하는 게 안전. agent 가 매번 명시적으로 commit 하지 않는 한.

## 9. Migration / lift

- W1 startup hook: 기존 ProductRow 마다 `init_product_workspace` (idempotent, github-bound 은 skip)
- e2e-hello cleanup: W1 deployment one-shot SQL. ProductRow 보존, 자식 데이터만 DELETE
- 기존 tests: `seeded_product` fixture 에 `await init_product_workspace` 한 줄 추가

---

**Next step**: PR W1 (workspace + worktrees + cleanup). 머지 로직 없이 dogfood.
