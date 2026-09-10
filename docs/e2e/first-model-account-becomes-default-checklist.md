# E2E — 첫 모델 계정이 워크스페이스 기본이 된다 (게이트 2)

낯선 사용자가 모델 계정 하나를 만들어도 워크스페이스 default 로 지정 안 하면
resolver 가 hard fail(`no_model_account`) — 첫 런이 멈추는 "숨은 계단". 첫 계정이
빈 default 슬롯을 자동으로 채우게 한다(기존 default 는 절대 안 덮음 → resolver 의
"never auto-stamps mid-life" 계약 유지).

## 검증

- [x] 헬퍼: unset 일 때만 set, 이미 있으면 no-op, 워크스페이스 없으면 no-op
- [x] REST `POST /api/v1/accounts`: 첫 계정 → workspace.default_account_id 자동 설정,
      둘째 → 불변
- [x] MCP `bsvibe_model_accounts_create`: 동일(파리티)
- [x] 헬퍼를 router-free identity 모듈(default_account.py)에 둬 import 계약 유지
      (mcp → identity 허용, mcp → router 금지; identity.service 는 router import 라 불가)
- [x] 절단 실증: 헬퍼 무력화 시 default 검증 3개 빨강
- [x] 기존 계정 테스트 12 무회귀(WorkspaceRow 없으면 no-op)
- [x] 백엔드 전체 · ruff/mypy(586)/import(5)
- [ ] 배포 후: 새 워크스페이스에서 첫 계정 생성 → default 설정 확인(라이브/로그)

## 범위

게이트 2 세 조각 중 **둘째(기본 계정 자동지정)** 완료. 남은 조각: Supabase 이메일
가입 정책(리포 밖 설정 — 코드로 불가, 형님이 Supabase 콘솔에서 결정).
