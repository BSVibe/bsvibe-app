# 실행 모드 기능 대조 — server_sandbox vs client_attach (2026-08-09)

> 목적: 클라이언트 레포 모드가 클라우드 모드와 **같은 계약**을 갖게 한다.
> 구멍을 하나 뚫는 게 아니라, 두 모드가 **틀어질 수 없는 구조**로 만든다.
> 계기: M5(BSVibe가 주간 리포트 → 텔레그램)가 client_attach에서 구조적으로 불가능함을 발견.

## 1. 대조표

에이전트가 실제로 쓸 수 있는 능력 기준.

| 능력 | server_sandbox | client_attach | 비고 |
|---|---|---|---|
| 파일 읽기/쓰기/편집/목록 | ✅ MCP `bsvibe_work_file_*` → 서버 샌드박스 | ✅ CLI 네이티브 → 파운더 트리 | **구현은 달라도 능력은 동등** |
| 셸 실행 | ✅ MCP `bsvibe_work_shell_exec` | ✅ CLI 네이티브 | 동등 |
| `declare_verification` | ✅ | ❌ | **보완됨** — in-place 파생 게이트(#705)가 대체 |
| `knowledge_search` (RAG 그라운딩) | ✅ | ❌ | 미보완. client_attach 런의 그라운딩 = 0 |
| `ask_user_question` | ✅ → Decision 생성, 런 일시정지 | ❌ | 미보완. 에이전트가 형님께 질문 불가 |
| `emit_deliverable` | ✅ → Deliverable → 커넥터 전달 | ❌ | 미보완. **M5 차단 지점** |
| `invoke_skill` | ❌ (executor 런) | ❌ | **모드 무관 기존 갭** — 아래 참조 |
| 커넥터 액션 툴 | ❌ (executor 런) | ❌ | **모드 무관 기존 갭** |

**client_attach 고유 손실은 4개, 그중 보완된 것은 1개(`declare_verification`)뿐이다.**

⚠️ `invoke_skill`·커넥터 액션 정정(2026-08-10): 이 둘은 `_drive_loop`가 **루프 레지스트리**에 등록하고
`tools_schema`로 넘긴다. 그런데 executor 어댑터는 `agentic=bool(tools)`로 **agentic 여부만** 판단하고
CLI의 실제 툴은 오직 MCP로 온다. ∴ **executor 런이면 두 모드 모두 못 쓴다** — client_attach 고유 갭이
아니라 executor 경로 전체의 기존 갭이며, 이 파리티 작업의 범위가 아니다(별도 이슈).
LiteLLM 등 진짜 function-calling 모델 경로에서만 살아 있다.

## 2. 왜 이렇게 됐나 — 두 가지가 한 스위치에 묶여 있다

[`adapter.py:692`](../Works/bsvibe-app/main/backend/dispatch/adapter.py)

```python
mcp = None if self.execution_target == "client_attach" else await self._work_tool_surface(...)
```

이 한 줄이 **서로 다른 두 질문**을 동시에 답한다:

1. **에이전트는 무엇으로 코드를 만지는가** — 서버 샌드박스(MCP) vs 파운더 트리(네이티브)
2. **에이전트는 BSVibe 플랫폼 능력을 갖는가** — 지식·질문·전달·스킬·커넥터

client_attach가 (1)에서 네이티브를 골라야 하는 건 맞다. 그런데 그 선택이 (2)까지 통째로 꺼버린다.
플랫폼 능력은 **소스가 어디 있든 무관**하다 — 서버가 제공하는 것이지 워크스페이스가 제공하는 게 아니다.

MCP를 끈 이유 자체는 정당했다: 에이전트에게 MCP 서면을 주면 CLI의 **자기 툴이 박탈**되므로
(`--disallowedTools`로 네이티브를 죽임) 네이티브 in-place 실행이 불가능해진다.
즉 "MCP를 주느냐"와 "네이티브를 쓰느냐"가 현재 배타적으로 구현돼 있다.

## 3. 함께 풀어야 하는 가드

[`claude_code.py`](../Works/bsvibe-app/main/backend/executors/worker/claude_code.py)의
`_exposed_tools_are_ours()`는 CLI가 실제로 노출한 툴을 `system/init` 이벤트로 읽어
**우리 툴 외에 뭐라도 있으면 태스크를 중단**시킨다(플래그를 믿지 말고 결과를 확인하라는 설계).

지금 계약은 "**오직** 우리 툴". client_attach 파리티는 "**네이티브 + 우리 플랫폼 툴**"이므로
이 가드도 모드를 아는 계약이 돼야 한다. 안 그러면 파리티를 켜는 순간 전 런이 중단된다.

## 4. 제안 — 툴 표면을 두 축으로 쪼갠다

현재 `RUN_TOOL_FORWARDING`은 이미 "단일 진실원천"으로 설계돼 있고(INV-7 #2:
*advertised ≡ registered*, 둘 다 이 튜플에서 파생), `knowledge_search`가 광고만 되고
등록이 안 돼 있던 사고를 이 원칙으로 막았다. 같은 원칙을 모드 축으로 한 번 더 적용한다.

```
WORKSPACE_TOOLS   file_read/list/write/edit, shell_exec
                  → 모드가 결정한다: server_sandbox=MCP, client_attach=네이티브

PLATFORM_TOOLS    knowledge_search, ask_user_question, emit_deliverable,
                  invoke_skill, 커넥터 액션
                  → 모드와 무관. 항상 MCP로 제공한다.
```

- `client_attach` = 네이티브 워크스페이스 툴 + **전체** 플랫폼 툴
- `server_sandbox` = MCP 워크스페이스 툴 + 전체 플랫폼 툴
- 노출 가드는 `허용 = 네이티브(모드가 허용할 때) ∪ 플랫폼 MCP 툴`을 **같은 소스에서 파생**

이러면 새 플랫폼 툴을 추가할 때 두 모드가 자동으로 같이 받는다. 한쪽에만 생기는 일이 구조적으로 불가능해진다.

`declare_verification`은 예외로 남긴다 — client_attach에는 in-place 파생 게이트라는 **더 정직한** 대체가
이미 있고(#705), 계약 선언은 서버측 워크트리를 전제하므로 워크스페이스 축에 속한다.

## 5. 검증 방법

- 대조표를 **테스트로 고정**한다: 모드별 노출 툴 집합을 단언해 한쪽만 늘거나 주는 일을 CI에서 잡는다.
- 라이브 E2E: client_attach 런이 `emit_deliverable`로 딜리버러블을 만들고 텔레그램까지 도달하는지.
  (2026-08-09 in-place verify 트랙에서 배운 것 — unit-green ≠ prod-works. PR 3개가 E2E로만 드러났다.)

## 6. M5에 대한 귀결

진행 문서의 "#691(제품별 secret 주입)이 해결되면 스케줄 등록만 남는다"는 **더 이상 정확하지 않다**.

| 모델 | Alpaca 키 | 딜리버러블·전달 |
|---|---|---|
| server_sandbox | ❌ #691 미해결 | ✅ |
| client_attach | ✅ `.env` 자연 상속 | ❌ 이 문서의 갭 |

두 모드가 M5에 필요한 절반씩만 갖고 있다. 파리티를 채우면 client_attach 경로로 M5가 열린다
(#691 없이). 그 외 M5 잔여 = 스케줄 등록(`cron_expr="30 9 * * 1"`, MCP `bsvibe_schedules_create` 사용 가능)
+ 클론에 Alpaca `.env` 배치. 텔레그램 커넥터/바인딩(`bf53298f`, chat `8242700007`, `output_mode=safe`)은 이미 등록됨.
