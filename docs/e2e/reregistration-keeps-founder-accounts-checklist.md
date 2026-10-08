# E2E — 워커 재등록은 형님이 만든 실행기 계정을 지키고 다시 묶는다 (#1075)

실측 2026-09-29: 09-21 `mac-mini-e2e` 재등록 뒤 design/implement 런이 전부 계정 해석 실패로 멈췄다. 형님이 손으로 만든
claude_code 계정(`litellm_model=opus`·`sonnet`, `extra_params.worker_id`=옛 워커)이 워커 해제 때 같이 지워졌고, 재등록은
`executor/<capability>` 만 다시 만들었다 → 규칙(`설계 → opus`·`구현 → sonnet`)과 워크스페이스 기본값(sonnet)이 고아. 경고 없음.

형님 결정(2026-10-08): 보존 + 같은 이름 재등록 시 재연결.

## 바뀐 것

- 해제(`revoke_worker`)는 등록이 만든 행(`litellm_model == executor/<executor_type>`)만 지운다. 손으로 만든 행은 남는다
- 등록(`register_worker_for_workspace`)은 **같은 이름의 비활성(해제된) 워커**의 손 계정 중 새 워커가 가진 기능의 것을 새 워커 id 로 다시 묶는다
  (`executor_account_adopted` 로그). 살아 있는 같은 이름 워커의 계정은 건드리지 않는다

## 검증 (로컬)

- [x] RED → GREEN — `tests/executors/test_reregistration_keeps_the_founders_accounts.py` (5: 보존 · 재연결 · 다른 이름 · 기능 없음 · 살아 있는 워커)
- [x] 전선 절단 5곳(해제 보존 · 재연결 호출 · 비활성만 · 기능 · 같은 이름) 각각 빨강
- [x] tests/executors · tests/mcp/test_workers_tools · tests/api 1206 passed · ruff · mypy

## 배포 후 (prod)

- [ ] 다음 워커 재등록(해제 → 같은 이름 등록) 뒤 `opus`/`sonnet` 계정이 남아 새 워커 id 를 갖고, `act → sonnet` 라우팅이 그대로

## 남은 틈

- 이름이 바뀐 재등록은 다시 묶지 않는다 (이름이 재등록의 정체성) — 그 경우 계정은 옛 워커 id 로 남는다
