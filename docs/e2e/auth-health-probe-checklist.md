# E2E — 인증 의존성은 로그인 없이 읽는다 (`GET /api/health/auth`)

prod 로그에 `supabase_token_failed` (warning) 가 약 62초마다 찍혔다. 출처는 버그가 아니라 2026-09-11 이후의 헬스 프로브다:

- launchd `com.blas1n.heartbeat` (`_infra/scripts/heartbeat.sh`, 60초) 와 GitHub `offbox-uptime.yml` (15분) 이
  "Supabase 가 살아 있나" 를 가짜 계정으로 `POST /api/auth/login` 을 보내서 읽었다 (4xx = 정상)
- 매 측정이 **실패한 로그인**이었다 — 진짜 로그인 실패·무차별 대입이 이 잡음에 묻히고, 모든 사용자 로그인이 백엔드 IP 하나로
  프록시되므로 프로브가 **사용자와 같은 per-IP sign-in rate limit** 을 썼다

형님 결정(2026-10-06): (c) — 프로브가 로그인을 쓰지 않게 한다.

## 바뀐 것

- `SupabaseAuthClient.health()` — GoTrue `GET /auth/v1/health` (apikey 헤더). 200 이면 True, 그 외·연결 실패면 False. 토큰 요청 없음
- `GET /api/health/auth` (공개) — 200 `{"status":"ok"}` / 503 `{"status":"unavailable"}`
- `.github/scripts/offbox-probe.sh` — deep 측정이 `GET /api/health/auth`, **200 만** 정상 (404 는 라우트 없음 — 정상이 아님)
- `_infra/scripts/heartbeat.sh` — 같은 경로로 (별도 리포 `blas1n/workstation`, 백엔드 배포 **뒤에** 바꾼다 — 먼저 바꾸면 404 로 하트비트가 끊겨 오프박스 경보가 울린다)

## 검증 (로컬)

- [x] RED → GREEN — `tests/auth/test_the_auth_dependency_has_its_own_health_reading.py` (7) · `tests/infra/test_offbox_probe.py` (9, 신규 2 + 재진술)
- [x] 전선 절단 4곳(상태 코드 검사 · 연결 실패 처리 · 503 · 분기), 각각 빨강 / 7 수집
- [x] ruff · mypy

## 배포 후 (prod)

- [ ] `curl -s -o /dev/null -w '%{http_code}' https://api.bsvibe.dev/api/health/auth` → 200
- [ ] heartbeat.sh 교체 후 `heartbeat.log` 에 실패 없음, healthchecks 핑 유지
- [ ] prod 백엔드 로그에서 `supabase_token_failed` 가 1분 주기로 더 찍히지 않음 (offbox 워크플로는 이 PR 머지로 바로 새 경로)
- [ ] 다음 offbox-uptime 실행이 `OK — /api/health 200 and /api/health/auth 200`

## 남은 틈

- GoTrue health 는 인증 서버가 살아 있는지만 본다. 비밀번호 로그인 경로만 고장난 경우는 못 잡는다 (예전 프로브도 4xx 를
  정상으로 봤으므로 같은 범위). apikey 가 틀리면 게이트웨이가 401 → 503 으로 잡힌다
