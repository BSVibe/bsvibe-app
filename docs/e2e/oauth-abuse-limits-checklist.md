# E2E — 미인증 OAuth 표면의 남용 한도 (그리고 그 한도가 쓰는 키)

두 가지다. **(1) 레이트리밋 키가 공격자 통제였다** — `request.client.host` 는
`ProxyHeadersMiddleware(trusted_hosts="*")` 가 `X-Forwarded-For` 의 **첫 항목**
에서 뽑는데, Cloudflare 는 그 헤더를 덮어쓰지 않고 **뒤에 붙인다**.
**(2) OAuth 표면에 남용 한도가 DCR 하나뿐이었다.**

> ⚠️ **이 문서의 `- [ ]` 는 "아직 안 걸어봤다"는 뜻이다 — "제품이 못 만든다"는
> 뜻이 아니다.** 아래 라이브 항목은 전부 *걸어보면 나올 수 있는 상태*인지
> 먼저 확인하고 적었다.

---

## A. 측정 — 고치기 전에 실제로 그런지부터

- [x] **배포된 prod 컨테이너 안에서** 미들웨어를 세워 실측했다. 읽기 전용
      (일회성 python 프로세스, 네트워크·DB·상태 변경 없음):

      ```
      $ docker exec bsvibe-prod-backend-1 /app/.venv/bin/python -c "import uvicorn; print(uvicorn.__version__)"
      0.47.0
      XFF '203.0.113.9'               -> client ('203.0.113.9', 0)   # 정직
      XFF '1.2.3.4, 203.0.113.9'      -> client ('1.2.3.4', 0)       # 공격자 값이 이긴다
      XFF '9.9.9.9, 203.0.113.9'      -> client ('9.9.9.9', 0)       # 요청마다 새 버킷
      XFF '198.51.100.7, 203.0.113.9' -> client ('198.51.100.7', 0)  # 피해자 버킷 오염
      ```

      근거는 uvicorn 0.47.0 의 `_TrustedHosts.get_trusted_client_address`:
      `if self.always_trust: return _parse_host_port(x_forwarded_for_hosts[0])`.

- [x] ingress 가 정말 Cloudflare 터널 하나인지 확인했다 —
      `cloudflared --config ~/.cloudflared/bsvibe-app.yml tunnel run`,
      `api.bsvibe.dev → http://127.0.0.1:8700`.
- [x] `grep -rn "429" backend/` 전수 — OAuth 표면의 남용 리미터는
      `_anon_dcr_rate_check` 하나뿐. 나머지 둘(`run_caps.py`,
      `v1/messages.py`)은 무료 플랜 동시 런 상한으로 **다른 축**이다.
- [x] `grep -rni "request\.client"` 전수 — 위조 가능한 값을 키로 쓰던
      곳은 `backend/api/oauth.py:850` **한 곳**뿐이었다.

## B. 유닛 / API — 자격증명 없이 돈다

### 리졸버 (`tests/shared/test_client_ip.py`, 9개)

- [x] `CF-Connecting-IP` 가 있으면 그 값이 이긴다 — `X-Forwarded-For` 의
      첫 항목이 다른 값이어도
- [x] ⭐ 서로 다른 위조 XFF 머리 셋이 **같은 키 하나**로 접힌다
      (`assert keys` 동반 단언 — 빈 집합에 통과하는 가드는 가드가 아니다)
- [x] 헤더가 없으면 소켓 피어로 폴백한다 (= 오늘의 동작, 회귀 없음)
- [x] 공백뿐인 헤더는 부재로 취급 · 값은 strip
- [x] 헤더도 피어도 없으면 `"unknown"` 센티넬 — 우회가 아니라 **하나의 버킷**
- [x] 부재가 `client_ip.cf_header_missing` (warning) 으로 **로깅**된다,
      `route` 와 함께
- [x] 그 로그가 헤더 집합을 덤프하지 않는다 (`Authorization` 값이 안 새는지 확인)
- [x] 헤더가 있으면 아무것도 로깅하지 않는다 — 음성 대조군

### 한도 (`tests/api/test_oauth_rate_limits.py`, 14개)

- [x] ⭐ **취약점 회귀 테스트**: 요청마다 다른 위조 XFF 머리를 달아도
      같은 `CF-Connecting-IP` 면 **버킷 하나**를 쓴다 (고치기 전엔 201, 지금 429)
- [x] ⭐ 남의 IP 를 XFF 에 적어도 **그 사람 예산을 못 쓴다**
- [x] `/token` — 기기 폴링 `50×2+10`회가 **전부** 통과한다
      (`authorization_pending`/`slow_down` 만 나온다, `assert answers` 동반)
- [x] `/token` — 실패한 자격증명 50회 뒤 51번째가 429
- [x] `/token` — 형식 오류(`unsupported_grant_type`)는 상한+5 회를 넘겨도
      버킷을 안 채운다
- [x] `/token` · `/introspect` · `/revoke` · `/device_authorization`
      각각: 상한 미만 정상 · 초과 429
- [x] 넷 다 **키별**이다 — IP A 가 예산을 태워도 IP B 는 정상 답을 받는다
- [x] `/device_authorization` — `invalid_scope` 프로브는 버킷을 안 채운다
      (검증 뒤에 리밋)
- [x] `GET /clients/by-client-id/{id}` 는 **안 막았다** — 상한의 3배를
      호출해도 전부 404 (독스트링이 공개인 이유를 변호하고 있고 client_id 는
      고엔트로피라 열거 오라클이 아니다)

**전선 절단 실증** — 10회, 전부 `py_compile` 로 컴파일 확인 + **돌아간
테스트 개수** 기록. 베이스라인 0 red / 77 ran:

| 끊은 것 | 돌아간 수 | RED |
|---|---|---|
| `/register` 를 `request.client.host` 로 되돌림 (**원래 취약점**) | 52 | 2 |
| 리졸버가 `CF-Connecting-IP` 를 무시 | 23 | 6 |
| 리졸버가 부재 로그를 안 남김 | 9 | 2 |
| `/token` 의 exhausted 검사 제거 | 30 | 2 |
| `_TOKEN_COUNTED_ERRORS = frozenset()` | 14 | 2 |
| **기기 폴링까지 센다**(순진한 요청 제한 버그) | 30 | 1 |
| `/introspect` 리미터 제거 | 14 | 2 |
| `/revoke` 리미터 제거 | 14 | 2 |
| `/device_authorization` 리미터 제거 | 30 | 2 |
| `/device_authorization` 이 scope 검증 **앞에서** 리밋 | 14 | 1 |

⚠️ **첫 하네스는 아무것도 증명하지 않았다.** `git checkout --` 로 복구하게
짜여 있었는데 대상 둘 중 하나가 **미추적 신규 파일**이라 git 이 pathspec
오류로 **아무것도 복구하지 않았다**. 독립이어야 할 절단 열 개가 조용히
누적됐고, red 개수가 단조 증가해서 오히려 그럴듯해 보였다. 바이트 스냅샷
복구로 바꿔 전부 다시 돌린 게 위 표다. **복구가 실제로 일어났는지도
측정하라** — 복구 명령의 종료 코드를 안 봤다.

## C. 라이브 — 배포 후 prod 에서 걸 것

아직 안 걸었다(이 PR 은 미배포). 걸어볼 수 있는 상태인지는 확인했다.

- [ ] `api.bsvibe.dev` 로 실제 요청을 보낸 뒤 백엔드 로그에
      `client_ip.cf_header_missing` 이 **안 뜨는지** 확인한다. 뜨면
      Cloudflare 가 `CF-Connecting-IP` 를 안 보내고 있다는 뜻이고,
      위조 가능한 키가 돌아온 것이다 — 이 로그가 그 신호로 존재한다.
- [ ] `audit.oauth.client_registered_anonymous` 의 `ip=` 가 실제 발신
      IP 로 찍히는지 (이 PR 의 부수 효과: 감사 로그도 이제 위조 불가 값을 쓴다)
- [ ] `bsvibe login` 을 prod 에 대고 끝까지 한 번 — 승인까지 일부러 몇 분
      끌어서 폴링이 100+ 회 돌게 한다. **한 번도 429 가 나오면 안 된다.**
      이게 Ⅲ 절 결정(요청이 아니라 실패를 센다)의 라이브 증명이다.
- [ ] 429 를 일부러 한 번 받아 본다 — 틀린 `device_code` 로 51회.
      ⚠️ 버킷은 **15분** 윈도우다. 같은 IP 에서 그 뒤 15분간 `/token` 이
      막히므로, 형님 본인 IP 로는 하지 마라.

## D. 알아 둘 것

⚠️ **버킷은 프로세스별 · 인메모리다.** 배포마다 초기화되고 레플리카
사이에서 합쳐지지 않는다. prod 가 백엔드 컨테이너 **하나**라서 오늘은
괜찮고, 수평 확장이 오면 **게이트 4 의 문제**다. 갈아끼울 seam 은
`_SlidingWindowLimiter` 하나다. 이 PR 은 Redis 리미터를 **안 만들었다.**

⚠️ **키는 퍼블릭 호스트네임 경로에서만 신뢰할 수 있다.** 이미 호스트의
`127.0.0.1:8700` 이나 LAN 에 닿는 호출자는 `CF-Connecting-IP` 를 지어낼
수 있다 — 다만 그 호출자는 이미 신뢰 경계 안이고 Cloudflare WAF 를 통째로
우회하는 위치다. 그리고 그 경우조차 **오늘보다 나쁘지 않다**: 오늘은
인터넷 어디서든 XFF 로 같은 일을 할 수 있다.

⚠️ **`trusted_hosts="*"` 는 건드리지 마라.** 바로 위 주석이 그게
`X-Forwarded-Proto` 를 위해 load-bearing 이라고 적어 뒀다 — URL 빌더가
Cloudflare 뒤에서 `https://` 를 뽑아야 한다. 좁히려면 Cloudflare egress
대역을 추적해야 한다. **키만** 옮겼다.

⚠️ **버킷은 프로세스 전역이므로 테스트 사이에 비워야 한다.**
`tests/conftest.py` 의 autouse 픽스처가 그 일을 한다. 없으면 앞 모듈이
채운 버킷 때문에 뒷 모듈의 첫 정직한 요청이 429 를 받고, 그 모듈이
테스트하는 것과 아무 상관 없는 이유로 빨개진다.

---

## 재현 명령

```bash
cd /Users/blasin/Works/bsvibe-app/main

# 1) 측정 — 배포된 미들웨어가 정말 첫 XFF 항목을 믿는가 (읽기 전용)
docker exec bsvibe-prod-backend-1 /app/.venv/bin/python -c \
  "import uvicorn; print(uvicorn.__version__)"     # 0.47.0
# 같은 스크립트를 로컬 .venv 로 돌려도 같은 표가 나온다

# 2) 유닛
uv run pytest tests/shared/test_client_ip.py tests/api/test_oauth_rate_limits.py -q

# 3) 전체 게이트 (프로브 PG 필요 — 포트를 정확히 확인할 것)
docker run -d --name ratelimit-probe-pg \
  -e POSTGRES_USER=bsvibe -e POSTGRES_PASSWORD=bsvibe -e POSTGRES_DB=bsvibe \
  -p 15529:5432 pgvector/pgvector:pg16
docker exec ratelimit-probe-pg psql -U bsvibe -d bsvibe -c "CREATE EXTENSION IF NOT EXISTS vector;"
export BSVIBE_DATABASE_URL="postgresql+asyncpg://bsvibe:bsvibe@localhost:15529/bsvibe"
export BSVIBE_MIGRATION_DATABASE_URL="$BSVIBE_DATABASE_URL"
export BSVIBE_APP_DB_PASSWORD=bsvibe_app_ci
uv run alembic upgrade head
uv run pytest --cov=backend --cov=plugin --cov-fail-under=80 -q
docker rm -f ratelimit-probe-pg        # ⚠ 끝나면 반드시
```

⚠️ **`_clean_all_rows` 는 가리킨 DB 의 모든 테이블을 DELETE 한다.**
포트를 반드시 확인하라 — `15442`(devcontainer-postgres-1) 나 prod 를
가리키면 지워진다.

⚠️ **`uv run pytest` 로 돌려라. `.venv/bin/python -m pytest` 는 다르다.**
첫 게이트를 후자로 돌렸더니 `tests/test_import_contracts.py` 둘이
`shutil.which("lint-imports") is None` 으로 빨개졌다 — 계약이 깨진 게
아니라 **`.venv/bin` 이 PATH 에 없었던 것**이다. 같은 시각 `lint-imports`
를 직접 돌리면 6 kept 0 broken 이었다. 게이트 재현은 런처까지 복사해야
한다.

⚠️ 런타임 DSN 을 owner 롤로 두면
`test_rls_is_active_layer3_for_the_runtime_role` 이 **정당하게** 빨개진다
("RLS 는 superuser 에게 무력"). 이 프로브는 단일 롤이라 그 하나가
예상된 빨강이고, **내 변경 탓이 아니다.**
