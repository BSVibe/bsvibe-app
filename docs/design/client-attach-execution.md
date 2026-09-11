# BSVibe Client-Attach Execution — Design (SoT)

> 상태: **구현 완료 (2026-08-06)** — 6-PR 전부 머지(#696~#701). E2E 실증 완료(BSVibe가 사용자 클론에서 네이티브 실행, `pwd`=클론 경로).
> 남은 옵션: in-place verify(워커 클론에서 derived gate 실행 → 정직한 `verified`). 현재는 `review_ready` + `proof_state=UNTESTED`.
>
> 원래 설계 착수: 2026-08-05. #692 "실행 위치를 제품/run 속성으로" 결정("둘 다 지원")의 client-attach 절반.
> 관련 이슈: #692(설계) · #691(서버측 secret, server-sandbox 절반) · #673(verbatim). dogfood: BStockReport M5.

## 1. 목표
run의 코딩 에이전트가 **사용자가 이미 작업하던 바로 그 디렉터리에서 그대로 이어서** 실행되는 모델을 추가한다. Claude Code local과 동형 — 사용자가 열어둔 워크스페이스에 BSVibe가 에이전트를 "붙여" 작업을 잇는다. env·toolchain·git 상태·파일은 **거기서 돌기 때문에 자연히 딸려온다**(별도 상속/주입 기능이 아니다). 실행 위치는 **제품/run 속성**으로 선언한다(형님 결정).

> **의도(형님, 2026-08-05)**: "그냥 사용자가 작업하던 공간에서 바로 이어서 하는 게 목적. env 상속 같은 잡다한 고려는 애초에 하지 않는다." → 워크스페이스는 **사용자의 실제 작업 디렉터리**이지 BSVibe가 만든 클론이 아니다. 이 한 줄이 아래 설계를 단순화한다.

**형님 결정 사항**
- 두 모델 다 지원, 실행 위치 = 제품/run 속성.
- client-attach 시 자격 노출 = **`.env` 전체 암묵 상속**(allowlist 아님. Claude Code local과 동일).
- 방향 = **대규모 재설계 진행**(client-attach가 T3로 인해 큰 작업임을 인지한 상태에서).

## 2. 현행 아키텍처 (T3 재설계 후)
```
에이전트 CLI (워커 호스트, 빈 temp cwd)
   │  파일/셸은 자기 툴이 아니라 …
   ▼
MCP work 툴 (bsvibe_work_file_write / shell_exec)  ← HTTP, run-scoped 토큰
   ▼
work-tool 팩토리: run → (workspace_dir = 서버측 worktree, sandbox = per-product DinD)
   ▼
서버측에서 실행. shell_exec는 sandbox 없으면 거부(work_tools.py:313).
```
- `worker/main.py:178-210`: 서버가 보낸 `workspace_dir`을 **의도적으로 무시**, CLI는 빈 temp 디렉터리에서 돎(cwd 용도뿐). T3가 "네이티브 로컬 툴 → 아무도 안 읽는 사설 복사본" 문제로 로컬 캡처를 제거하고 MCP-only로 감.
- ∴ `adapter.py:663`의 `workspace_dir="."`는 **vestigial**. #692 본문의 "절반 깔린 배관" 전제는 무효(이슈에 정정 코멘트 게시).

## 3. Client-attach 실현 방식 — 설계 포크

### (A) 네이티브 로컬 툴 [확정]
`client_attach` run은 CLI를 **네이티브 툴(Read/Write/Bash)**로 **사용자가 지정한 기존 작업 디렉터리**에서 그대로 실행. 그 디렉터리는 사용자가 이미 쓰던 공간이므로 env·`.env`·venv·toolchain·현재 git 상태가 **전부 그 자리에 이미 있다** — 상속/주입 배선이 필요 없다. 에이전트는 사용자의 작업을 "이어서" 편집·실행한다.
- 장점: Claude Code local과 동형. env·toolchain 무료(그 디렉터리에서 도니까). server→worker 콜백 채널 불필요. 네이티브 툴은 검증된 표면. verify/gate가 **실제 사용자 환경에서** 돎 — client-attach의 존재 이유와 정합.
- live 작업 트리 처리: raw Claude Code가 그렇듯 **에이전트(CLI)가 사용자 live 상태(uncommitted·현재 브랜치)를 스스로 판별해서 처리**한다. BSVibe가 WIP 보호 기계를 따로 만들 필요 없음 — 네이티브 툴에 딸려오는 동작.
- 유의: T3가 제거한 로컬-툴 실행을 client_attach에 한해 되살림(캡처는 §5 PR4로 경량).

### (B) 워커측 MCP 실행
MCP work 툴은 유지하되 `client_attach` 시 실행 표면을 워커 머신으로 라우팅. MCP 핸들러는 서버에서 도므로 server→worker 역채널 또는 워커-호스트 실행 서비스 필요.
- 장점: 툴 표면 일관(MCP 어디서나). 서버가 오케스트레이션 유지.
- 단점: server→worker 실행 채널/워커 로컬 실행 서비스 신설 = 큰 인프라. `.env` 상속도 그 채널을 통해 별도 배선.

**추천 = (A)**. 이유: `.env` 전체 상속이 subprocess로 공짜, 새 네트워크 채널 불필요, verify가 사용자 toolchain에서 도는 게 client-attach의 목적과 일치. (B)는 일관성 이득 대비 인프라 비용이 과다.

## 3.5 ⭐ 프라이버시 계약 — client_attach는 서버에 소스를 노출하지 않는다 (형님, 2026-08-06)
client_attach를 고른 사용자는 **BSVibe 오케스트레이션은 쓰되 소스를 서버에 노출하지 않겠다는 의지**일 수 있다. 명시적 변경이 없는 한 **서버측에 소스를 clone·저장·knowledge ingest·worktree 생성하면 안 된다.** 소스는 전적으로 사용자 머신에만. 서버는 오케스트레이션 메타데이터(run 상태·task 디스패치)만 보유.

**영향 (client_attach 제품 전반, PR5/6로 구현):**
- **Run 실행**: 서버 worktree provisioning(`agent_runtime._product_workspace_provisioner`→`add_run_worktree`) **skip**. 현행은 product-bound run마다 서버측 repo 클론 → 소스 노출. client_attach면 no-op.
- **Verify/terminal(PR5)**: 서버 verify/merge 없음 → **review_ready**로 종료(워커 task 성공 신뢰, 형님 결정). no-work nudge도 skip(네이티브 쓰기는 서버 불가시).
- **Bootstrap**: 서버측 clone+ingest **skip**(client_attach면). ⚠️ bstockreport는 이 결정 前 이미 ingest됨.
- **run.payload에 execution_target 미탑재** → provisioner/drive loop가 gate하려면 threading 필요(agent_runtime은 이미 execution_target resolve함 — 그 지점서 provisioner gate 가능).

## 4. 보안 모델
- **신뢰 경계**: client-attach는 샌드박스 격리를 포기하고 **사용자 머신을 신뢰**한다. 에이전트는 사용자 `.env` 전체 + 로컬 파일시스템 접근을 얻음.
- **선언**: `client_attach`는 **제품별 명시 opt-in**(기본 `server_sandbox`). 실수로 사용자 머신에서 임의 코드가 돌지 않게.
- **Safe Mode**: durable/outbound 액션(딜리버러블·커넥터 전송·push)은 기존 safe_mode 승인 게이트를 그대로 통과. 로컬 실행이라도 외부로 나가는 것은 승인 대상.
- **워커 바인딩**: `client_attach` 제품은 특정 워커(등록된 사용자 머신)에만 디스패치. 아무 워커나 사용자 코드를 실행하지 않게 워커 capability/바인딩으로 제한.

## 5. 단계별 구현 (PR 분할)
- **PR 1 (foundation, 포크 무관)**: `ProductRow.execution_target` enum(`server_sandbox` 기본 | `client_attach`) + 마이그레이션. run payload에 전파. MCP tool + PWA에서 설정. **동작 변화 없음**(선언만). ← 지금 착수.
- **PR 2 (dispatch 전파)**: run의 `execution_target`을 task payload에 실어 워커가 인지.
- **PR 3 (워커: 사용자 작업 디렉터리)**: `client_attach` task는 빈 temp 대신 **제품에 등록된 사용자 작업 디렉터리**를 cwd로, CLI를 네이티브 툴로 실행(env는 그 자리에서 자연 상속). 클론 안 함 — 사용자가 이미 쓰던 그 공간.
- **PR 4 (캡처, 경량)**: 에이전트 변경은 그 디렉터리에 그대로 남는다 — raw Claude Code처럼 에이전트가 live 상태를 스스로 판별해 처리. BSVibe는 WIP 보호 기계를 만들지 않음. per-run 격리 클론/verify-merge 기계는 이 모델에 과함. (필요 시 최소한의 결과 표식만.)
- **PR 5 (client_attach terminal + 서버 소스 노출 차단)** ⭐ 확장됨:
  · run.payload에 execution_target 탑재(또는 agent_runtime서 resolve해 orchestrator에 전달).
  · client_attach면 **서버 worktree provisioner skip**(소스 노출 방지).
  · drive loop: client_attach면 no-work nudge·서버 verify skip → **review_ready** 종료(워커 task 성공 신뢰). 형님 결정.
  · E2E 실증됨: 네이티브 실행은 작동하나 서버 루프가 written_paths 비어 무한 nudge/정체 → 이 PR이 종료 unblock.
- **PR 6 (safe boundary + bootstrap)**: client_attach 제품별 opt-in(있음) + 워커 바인딩 + safe_mode 정합 + **bootstrap 서버 clone/ingest skip**(client_attach).
- **후속: in-place verify (2026-08-06 착수)** — derived gate를 워커의 사용자 디렉터리에서 실행 → 정직한 `verified`.

## 8. In-place verify 설계 (2026-08-06)
**문제**: 현재 client_attach는 `review_ready`+`proof_state=UNTESTED`에서 멈춘다. 서버는 소스가 없어 gate를 못 돌린다. 그런데 gate의 정직성은 **"명령의 exit code가 판정"**(모델 의견 아님)이므로, 명령을 사용자 머신에서 돌리면 그대로 성립한다.

**핵심 통찰**: 워커에 **결정적 명령 채널**만 있으면 된다. 기존 dispatch/await/result 인프라(executor_tasks + redis stream + `/api/v1/workers/result`)를 그대로 재사용할 수 있다 — 새 네트워크 채널 불필요.

**설계**
- **워커**: `action="exec"` 브랜치 추가. `prompt`를 셸 명령으로 보고 workspace_dir(=사용자 디렉터리)에서 실행, exit code + stdout/stderr를 결과로 POST. (기존 `action="execute"`(CLI 에이전트) / `"cancel"`과 나란히.)
- **서버**: `ClientWorkerSandboxSession` — `SandboxSession` Protocol(`workspace_mount`/`exec`/`read_file`/`list_dir`)을 워커 exec 태스크로 구현. verify가 쓰는 인터페이스가 그대로라 **verify 로직은 무변경**.
- **verify 배선**: client_attach run이면 DinD box 대신 이 세션을 넘긴다 → `_read_repo_manifests`·`_run_derived_gate`가 사용자 머신에서 돈다.
- **터미널**: gate가 실제로 돌면 `proof_state=PROVED` 정직하게 도달 가능(현재의 UNTESTED 대체).

**프라이버시 유지**: 명령과 exit code/출력만 오간다. 소스 파일은 서버로 오지 않는다(단 gate 출력에 코드 일부가 포함될 수 있음 — 기존 sandbox verify와 동일한 수준).

**PR 분할**: A) 워커 exec 액션 → B) `ClientWorkerSandboxSession` + verify 배선.

각 PR은 TDD(RED→GREEN), 워크트리 격리, 실 PG 검증, 순차 머지.

### 8.1 구현 완료 (2026-08-06, 4 PR)
| PR | 내용 | 상태 |
|---|---|---|
| #702 | A/2 — 워커 `action="exec"`: 셸 명령 1회 실행 + exit code 보고 | ✅ 머지 |
| #703 | B/2a — `ClientWorkerSandboxSession`/`Manager`(Protocol 구현) + `dispatch_task(action=)` | ✅ 머지 |
| #704 | B/2b — 런별 샌드박스 선택 배선 + 파운더 트리 프로비저닝 금지 | ✅ 머지 |
| #705 | B/2c — 파생 게이트 실제 실행 + 정직한 `PROVED` | ✅ 머지 (`aa59ba9`, 2026-08-09) |
| #716 | E2E 발견 — 게이트 box를 파운더 디렉터리에 고정 | ✅ 머지 (`e7ab131`) |
| #717 | E2E 발견 — 게이트가 git으로 실제 변경 파일을 파악 | ✅ 머지 (`112cc4fb`) |

**⚠️ 설계 수정 (구현 중 발견)**: §8의 "verify 배선 = DinD box 대신 이 세션을 넘긴다"는 **그대로는 안 된다**.
client_attach는 MCP 워크툴이 차단(#700)돼 에이전트가 `declare_verification`을 할 수 없다 → `assemble_contract`가
**항상 `None`** → 일반 verify 경로는 `no_verification_declared` **결정으로 빠져 오히려 퇴보**한다.
∴ **파생 게이트 전용 경로**(`application/inplace_gate.py`)로 구현했다 — 파생 게이트는 선언된 계약이 불필요하고
레포 자신의 매니페스트에서 명령을 유도하므로, 여기서 작동하는 유일한 검증 메커니즘이다.

**정직성 사다리(전부 fail-CLOSED)**: 매니페스트 없음=게이트 부재(UNTESTED 유지) / 유도 실패=`passed=False`,
절대 PROVED 아님 / 명령이 돌아 실패=정직한 실패로 에이전트에 피드백 후 재시도 / exit 127=`unavailable`
(실패도 증명도 아님) / **실제로 돌아 통과=PROVED**. `PROVED`는 주장이 아니라 `VerificationResult` 행에 남는
**조회 가능한 증거**(무엇이 어떤 exit code로 돌았는지).

**함정 회피**: 긴 외부 호출(LLM 유도 + 원격 명령) 직전 `_release_connection` — 열린 txn 안 긴 await는
BSVibe 반복 outage(#632·#686·#680). 게이트는 **에이전트 턴이 돌았던 그 워커에 pin**(그 머신만 소스를 가짐).
전제조건 불완전(redis/session_factory/경로/executor 계정 없음)이면 default 매니저 유지 = 오늘 동작 —
게이트가 **조용히 엉뚱한 머신**에서 도는 일만은 봉쇄.

### 8.2 ✅ E2E 실증 완료 (2026-08-09) — 그리고 그 과정에서 나온 프로덕션 결함 2개

**결과**: BStockReport(client_attach) run `66ab2d45` → `proof_state=PROVED`,
`origin=derived_in_place`. 파운더 머신에서 실제로 돌아간 명령:

| 명령 | kind | exit |
|---|---|---|
| `uv run pytest tests/test_baseline.py -v` | test | 0 |
| `uv run ruff check src/bstockreport/baseline.py tests/test_baseline.py` | quality | 0 |

명령이 **변경된 파일로 정확히 스코프**된 것이 #717이 작동한다는 증거다.

**unit-green 상태로 배포된 결함 2개를 E2E가 잡았다. 둘 다 같은 병이다 —
테스트가 프로덕션이 주지 않는 값을 자기가 넣어줬다.**

**① 게이트가 엉뚱한 머신 경로로 나갔다 (#716)**
`ClientWorkerSandboxManager.acquire(project_id, workspace_path)`가 호출자 인자를 그대로 썼는데,
실제 호출자 `agent_loop`는 run의 **서버측** `/app/var/runs/<run_id>`를 넘긴다. 파운더 머신엔 없는 경로다.
→ 모든 게이트 명령이 `client_attach_workspace_missing`으로 실패 → 매니페스트 0개 →
**"이 레포엔 게이트가 없음"으로 둔갑**해 UNTESTED. 기존 테스트는 `acquire(id, "/Users/founder/proj")`처럼
**원하는 답을 인자로 직접 넘겨** 호출했으므로, 인자를 되돌려주는 매니저를 영원히 통과시켰다.
∴ 어디서 실행하는지는 **dispatch 컨텍스트**(제품에서 옴)이므로 매니저가 소유한다.

**② 파생기에게 "변경 없음"이라는 거짓을 단언했다 (#717)**
`run_inplace_gate`가 `written_paths=[]`를 넘겼고, 프롬프트엔 `(no files changed)`로 들어갔다.
파생기는 시스템 프롬프트대로 `applicable=false`(검증할 게 없음)를 냈다.
그러나 `written_paths`는 client_attach에서 **항상** 빈다 — 에이전트가 네이티브 툴을 쓰므로 서버가
쓰기를 관측하지 못할 뿐이다. 사실은 **"서버가 볼 수 없다"**인데 **"변경이 없다"**를 단언한 것.
∴ 파운더 트리에게 git으로 묻는다: run 진입 **전** `git rev-parse HEAD`를 baseline으로 잡고,
종료 시 `git status --porcelain` ∪ `git diff --name-only <baseline>..HEAD`. **파일명만** 오가므로
§3.5 프라이버시 계약 유지. 진짜로 아무것도 안 바꾼 run은 여전히 빈 목록 → PROVED 못 얻음(양방향 정직).

**정직성 사다리는 두 사고 모두에서 제 역할을 했다** — 거짓 PROVED는 한 번도 나오지 않았다.
다만 ①에서 **배선 결함이 "게이트 부재"라는 정직한 결론으로 위장**됐다. 인프라 오류와 진짜 부재를
구분하지 못하는 것이 남은 약점이다(아래).

### 8.3 ✅ 부재가 스스로를 증명한다 (#718, `da9a46f`, 2026-08-09)

**① 프로브**: 매니페스트를 읽기 **전에** `box.list_dir(".")`. 실패하면 `passed=False` +
`workspace_probe_failed`로 fail-closed 기록(파생기도 안 부름). 성공했는데 매니페스트가 0개일 때만
진짜 gateless(`None`). ∵ `_read_repo_manifests`는 읽지 못한 파일을 전부 건너뛰므로 **닿지 않는 머신**과
**빌드 설정 없는 레포**가 똑같이 0개로 도착하는데, "gateless" 판정은 후자만의 것이다.

**② 인프라 실패를 에이전트에게 떠넘기지 않는다**: 기존 피드백 분기는 `not gate["passed"]`만 봐서,
명령이 **하나도 안 돈** 경우(파생기 실패·프로브 실패)에도 `"게이트가 실패했다: []"`를 보내 고칠 수 없는
것을 고치라며 남은 사이클을 태웠다. `gate_failure_is_actionable()` = **실제로 RAN 하고 failed 한 명령이
있는가**. exit 127(`unavailable`)도 제외 — 그 머신에 툴이 없는 것이지 변경이 깨뜨린 게 아니다.

**LOC**: `_drive_loop.py`가 610줄로 천장(600) 초과 → 줄이는 대신 **응집 블록 분리**:
client_attach 종료 판단 전체 → `_loop_context.settle_client_attach`. 571줄.

**회귀 확인**: run `ac013bfa` → `proof_state=PROVED`, `workspace_probe_failed` 없음.
프로브가 정상 경로를 막지 않는다(이 변경의 위험은 거짓 실패 쪽이었으므로 성공 케이스로 확인).

### 8.4 E2E 4회 기록
| run | 과제 | 결과 |
|---|---|---|
| `27e462d5` | report.py `수집 실패: None` | ❌ 게이트가 서버 경로로 나감 → #716 |
| `3fb8873c` | delivery.py 긴 줄 미분할 | ❌ 변경목록 미전달 → `applicable=false` → #717 |
| `66ab2d45` | baseline.py SQLite URI 이스케이프 | ✅ **PROVED** |
| `ac013bfa` | llm.py 닫히지 않은 `<think>` 누출 | ✅ **PROVED** (#718 회귀 확인) |

부수 효과로 BStockReport 클론에 실제 결함 수정 4건이 커밋됐다(`15ff389`·`b491fae`·`b5226d5`·`21e167d`,
전부 push 안 된 로컬 커밋 — 유지/폐기는 형님 판단).

## 6. 결정 (형님, 2026-08-05)
- **(A) 네이티브 로컬 툴 확정**. 워크스페이스 = **사용자의 기존 작업 디렉터리 확정**(BSVibe 클론 아님).
- **⭐ 실행 config는 전부 제품에 붙는다. 워커는 순수 LLM 실행 엔진일 뿐이다.**
  - `execution_target`·`client_workspace_path` 등 "어디서/어떻게"는 **제품 등록 시 지정**(제품 metadata). 워커는 client-attach 상태를 하나도 보유하지 않음 — task payload가 시키는 대로만 실행.
  - 이유: 그래야 **API 방식 사용자도 동일하게** 기능(워커 등록에 config를 굽지 않음). 제품이 SoT.
  - ∴ 워커 register UX는 건드리지 않음. 경로도 워커가 아니라 **제품**에 등록.
- **남은 것**(PR 진행 중 확정): (1) 사용자 live 작업 트리 캡처 — raw Claude Code처럼 에이전트가 처리(경량). (2) verify in-place 시 서버 정직성 등급 재검토.

## 7. 전달 배관 (PR2, 발견 완료)
- dispatched payload = flexible dict(Redis Streams flat strings). `workspace_dir`·`repo_url`은 **이미 워커로 전송**(worker가 workspace_dir는 현재 무시).
- `ExecutorTaskRow`엔 free-form JSON 없음 → `execution_target`을 명시 전달하려면 **순수 String(32) 컬럼**(server_default `server_sandbox`, enum 아님 → 안전 마이그레이션, fresh-PG 스모크). 경로는 기존 `workspace_dir` 필드 재사용(client_attach면 제품의 client_workspace_path, 아니면 `.`).
- 값 출처: 제품 metadata → agent_runtime이 resolve → resolver → adapter → create_task → task row → dispatch payload → (순수)워커. `repo_url`(Lift E32)과 동일 경로.

## 7. BStockReport M5 귀결
client-attach가 서면, bstockreport 제품을 `client_attach`로 표시 + Mac Mini 워커 바인딩 → 주간 run이 이 머신에서 `.env`(Alpaca 키) 상속하여 실행. 텔레그램 커넥터(`7bfb9d70`)·바인딩(`bf53298f`)은 이미 등록. launchd는 백업으로 유지.
