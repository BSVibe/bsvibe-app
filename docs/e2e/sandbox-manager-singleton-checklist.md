# E2E — 샌드박스 매니저 싱글턴 (E1 fix)

INV-7 #4. MCP work-tool transport가 툴 호출마다 샌드박스를 재생성하던 결함 수정.

## 배경
- 결함: `_sandbox_for`가 `build_sandbox_manager()`(비캐시)를 매 MCP 툴 호출마다 호출 → `_containers` 캐시 항상 빔 → `acquire`가 매번 `docker rm -f` + `docker run`.
- 실측(2026-07-14, 프로덕션): 같은 `project_id`로 `sandbox_created`가 툴 호출마다 반복.
- 수정: `_sandbox_for`·`agent_runtime._resolve_sandbox_manager` 둘 다 `get_sandbox_manager()`(프로세스 싱글턴) 경유. `build_sandbox_manager`는 싱글턴/유닛테스트 전용으로 docstring 명시.

## 체크리스트

- [x] **RED**: `test_sandbox_created_once_across_many_tool_calls` — 수정 전 3회 생성으로 실패, `sandbox_created` 3회 로깅 확인
- [x] **GREEN**: 같은 테스트가 컨테이너 생성 1회로 통과 (3 툴 호출)
- [x] **싱글턴 동일성**: `_sandbox_for` 반복 호출이 같은 매니저 인스턴스(`_mgr`) 반환
- [x] 회귀: 샌드박스 관련 전 스위트 (resolver/docker_manager/reaper/routing/work_registry) green
- [x] 회귀: workflow/dispatch/glue/executors 1320 passed
- [x] lint + format + mypy (변경 3파일) clean
- [x] `agent_runtime`의 no-silent-host-fallback 안티리그레션 유지 (routing 테스트 patch 대상만 `get_sandbox_manager`로 갱신, raise 동작 불변)
- [ ] **프로덕션 재실증**: 실제 executor 런 구동 → DinD 내부에서 `bsvibe-sbx-<project>` 컨테이너가 **런 동안 재생성되지 않고 유지**되는지 (`docker exec bsvibe-sandbox-dind docker ps`의 CREATED/uptime), `sandbox_created` 로그가 툴 호출마다 반복되지 않는지 확인

## 알려진 잔여 (범위 밖, 별도 lift)
- **cross-process 경계**: MCP 툴은 API 프로세스, verify는 워커 프로세스 → 각자 싱글턴. verify 크로싱마다 컨테이너 소유권이 바뀌며 1회 재생성 가능. per-tool-call thrash(이번 수정)와 별개로, 프로세스 간 컨테이너 공유는 후속 설계 필요.
- **reaper 미기동**: `sandbox_reaper_loop`가 프로덕션에서 `create_task`되는 곳이 없음 → idle 컨테이너 미회수(누수). 별도 half-wired 결함.
