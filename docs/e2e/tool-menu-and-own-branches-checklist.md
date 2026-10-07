# E2E — 도구 메뉴는 있는 것만 말하고, 자기 PR 스킵은 브랜치 규칙 셋을 다 안다 (#1116)

- `bsvibe_run_routing_rules_list` 설명이 *"design → executor/codex … Distinct from bsvibe_routing_rules_*"* 였다. codex 는 BSVibe
  도구를 받을 수 없어 에이전트 작업이 거부되고(`dispatch.adapter`), `bsvibe_routing_rules_*` 도구는 테이블과 함께 지워졌다.
  에이전트·MCP 클라이언트가 읽는 메뉴가 죽은 실행기를 권하고 없는 도구를 가리켰다.
- 런 브랜치 규칙이 셋인데(`bsvibe/run/<uuid>` · `bsvibe/run-<8hex>` · client_attach `run/<8hex>`) #1101 의 자기 PR 스킵은 앞의 둘만
  알았다. client_attach 런의 PR 은 형님 토큰으로 열려 sender 가 USER 라서, 브랜치가 유일한 표시다 — 놓치면 자기 PR 이 새 런이 된다.

## 바뀐 것

- 설명문·모듈 docstring 정정 (예시를 `executor/claude_code` 로, 없는 도구 언급 제거)
- `plugin/github/webhook.py` — `run/<8hex>` 를 **정확히** 일치로 자기 브랜치로 인식 (`run/fix-login` 같은 사람 브랜치는 아님)
- codex 라우팅은 이미 디스패치에서 명시적으로 거부된다(에이전트 작업만 — frame·judge 같은 도구 없는 호출은 여전히 가능). 그래서 규칙
  작성 단계에서 막지는 않았다
- 브랜치 규칙 단일화는 하지 않았다 — 형님 머신의 기존 워크트리 이름이 바뀌므로. 스킵에 셋째를 더했다

## 검증 (로컬)

- [x] RED → GREEN — `tests/mcp/test_the_tool_menu_names_only_what_exists.py`
  - 모든 도구 설명이 언급하는 `bsvibe_*` 이름은 등록된 도구(또는 `_*` 패밀리)여야 한다 — **모든 도구에 대한 가드**
  - 설명이 가리키는 `executor/<x>` 는 BSVibe 도구를 받을 수 있는 실행기여야 한다
  - `worktree_branch(run_id)` 는 자기 브랜치 / 사람 브랜치 3종은 아님
- [x] 전선 절단 4곳 각각 빨강
- [x] tests/mcp + GitHub 웹훅·배송 glue 409 passed · ruff · mypy · lint-imports

## 배포 후 (prod)

- [ ] `bsvibe_run_routing_rules_list` 설명에 codex·`bsvibe_routing_rules_*` 가 없다 (MCP tools/list)
- [ ] 다음 client_attach(BStockReport) 런이 PR 을 열 때 그 PR 웹훅이 새 런을 만들지 않는다
