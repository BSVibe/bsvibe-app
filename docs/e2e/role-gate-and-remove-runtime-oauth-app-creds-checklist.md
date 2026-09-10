# E2E — 권한 게이트 + 런타임 OAuth 앱 creds 쓰기 제거 (H3)

두 결함이 같은 축(require_role 미적용):
(a) 배포 전역 OAuth 앱 creds 를 아무 인증 멤버가 덮어씀(tenant→instance 상승)
(b) viewer 가 safe_mode 를 끄거나 워크스페이스를 삭제(멤버십만 검사, role 무시)

## 검증

- [x] (a) REST `POST /{provider}/app-credentials` 라우트 제거(부재 가드: 앱 라우트 목록에 없음)
- [x] (a) MCP `bsvibe_connectors_set_oauth_app` 툴 제거(unknown tool)
- [x] (a) service `set_app_credentials` 제거. 양성 대조군: github `upsert_app_credentials` + env `register_configured_providers` 생존
- [x] (b) 단수 PATCH `require_role("admin")` — viewer/editor safe_mode 토글 403, admin/owner 200
- [x] (b) 복수 PATCH `_owned_workspace(minimum_role="admin")`, DELETE `owner` — viewer/editor/admin DELETE 403
- [x] 두 라우터 다 거부 시 행 무손상(safe_mode True 유지, deleted_at None)
- [x] `test_scope_audit` 낡은 app-credentials allowlist 항목 제거(가드 정직성)
- [x] 전체 스위트 6716 passed, ruff/format/mypy(583) 통과
- [ ] 배포 후: prod 3/3 owner 라 기존 흐름 무영향 — 회귀 관측만

## UX 변경 (env 전용)

slack/notion/discord 의 OAuth 앱 creds 는 이제 **env 로만** 설정(`register_configured_providers`).
런타임 API(REST/MCP)로 설정하던 흐름은 제거됨. github(manifest)·sentry(install) 무관.
prod 미사용이라 라이브 영향 없음.
