# E2E — 샌드박스 컨테이너는 런 하나의 것이다 (#1107)

컨테이너가 제품 단위(`bsvibe-sbx-<제품 id>`)였고, 처음 만든 런의 워크트리를 `/work` 로 마운트했다. 같은 제품의
`server_sandbox` 런 두 개가 동시에 돌면 두 번째 런의 `shell_exec`·검증이 첫 번째 런의 워크트리에서 돌고, 먼저 끝난 런의
`release` 가 다른 런의 컨테이너를 내렸다.

같은 DinD 를 두 프로세스가 같은 이름으로 쓴다 — 워커(`RunOrchestrator`: 런 시작에 acquire, 끝에 release)와
API(MCP 작업 도구). 캐시가 따로라서 API 의 첫 도구 호출이 워커의 컨테이너를 `rm -f` 하고 다시 만들었다.

형님 결정(2026-10-06): **런 단위 + 입양 + 정리.**

## 바뀐 것

- 키가 런 id — `agent_loop`(acquire/release) · `work_registry._sandbox_for`. `merge_watch_client_box` 는 원래 런 id
- 캐시된 박스는 **같은 워크트리**일 때만 재사용
- 캐시에 없어도 같은 이름의 컨테이너가 **돌고 있고 같은 워크트리**면 입양(`sandbox_adopted`) — 지우지 않는다. 입양한 박스는
  이 프로세스의 슬롯을 쓰지 않고, 유휴 회수는 잊기만 한다. 런 종료(`release`)는 누가 만들었든 지운다
- 슬롯이 꽉 찼을 때, 이 프로세스가 만든 박스 중 이미 사라진 것(다른 프로세스가 지움)의 슬롯을 먼저 놓는다
- `docker inspect` 템플릿(`{{.State.Running}}|…Mounts…/work…Source`)을 실제 데몬에서 확인 — `true|<마운트 경로>`

## 검증 (로컬)

- [x] RED → GREEN — `tests/supervisor/sandbox/test_a_sandbox_belongs_to_one_run.py` (RED 6, 대조군 2)
- [x] 전선 절단 6곳(워커 키 · API 키 · 워크트리 일치 · 입양 · 슬롯 정리 · 입양 회수), 각각 빨강 / 8 수집
- [x] 관련 스위트 602 passed (supervisor · mcp · run_orchestrator · glue 샌드박스 · client_attach)
- [x] ruff · mypy

## 배포 후 (prod)

워커 컨테이너도 재배포된다 (backend 와 같은 이미지).

- [x] 다음 BSVibe(server_sandbox) 런에서 DinD 컨테이너 이름이 `bsvibe-sbx-<런 id>` — 실측 2026-10-07 런 `4414bcd5`·`b2ebd3c4` (`sandbox_created` → 끝나고 `sandbox_removed`)
- [ ] (미관측 — 머지 재배포로 그 시점 API 로그가 사라짐) API 로그에 `sandbox_adopted` (워커가 만든 박스를 API 가 입양) — `sandbox_created` 가 런당 한 번
- [ ] 같은 제품 런 두 개 동시 — 각자 자기 워크트리에서 명령이 돈다

## 남은 틈

- 동시 상한 `sandbox_max_concurrent=2` 는 이제 **런 수**다. 셋째 server_sandbox 런은 슬롯이 빌 때까지 기다린다(프로세스별)
- API 가 먼저 만들고 워커가 입양한 경우, 워커의 `release` 가 지우지만 API 의 슬롯은 다음 슬롯 부족 때(또는 유휴 회수 때) 풀린다
