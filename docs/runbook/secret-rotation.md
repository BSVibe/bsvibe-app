# 런북 — 시크릿 로테이션

**상태(2026-09-23)**: 이 문서는 **측정으로 쓰였고, 아직 한 번도 실제로 걸어본 적이 없다.**
§2b(워커 토큰)가 2026-09-23 에 추가됐다 — 걸을 예정인 첫 절차다.
각 절차의 검증 여부를 항목마다 명시한다. 걸어본 뒤에는 그 표시를 갱신하라.

> ⚠️ 이 런북의 목적 절반은 **"돌릴 수 있다"가 아니라 "무엇이 못 돌아가는가"를 적는 것**이다.
> 시크릿 하나(`BSVIBE_GATEWAY_KMS_KEY_B64`)는 **절차만으로 돌릴 수 없고, 잘못 돌리면 실패가
> 조용하다.** 그걸 모른 채 "순서대로 하면 된다"고 적힌 문서가 가장 위험하다.

---

## 0. 먼저 — 이 표부터 읽어라

| 시크릿 | 절차로 돌아가나 | 블래스트 반경 | 실패가 시끄러운가 |
|---|---|---|---|
| `BSVIBE_APP_DB_PASSWORD` | ✅ 자가 적용 | 백엔드·워커의 모든 DB 연결 | **시끄럽다** — 연결 거부 |
| `BSVIBE_DB_PASSWORD` (owner) | ⚠️ **수동 `ALTER ROLE` 필요** | 마이그레이션·부트 | **시끄럽다** — 부트 실패 |
| `BSVIBE_REDIS_PASSWORD` | ✅ | 워커 디스패치·스트림 | **시끄럽다** |
| `BSVIBE_OAUTH_PRIVATE_KEY_PEM` | ⚠️ 미검증 | 발급된 액세스 토큰 전부 무효화 | 시끄럽다(401) |
| `BSVIBE_PRODUCT_BUNDLE_S3_*` | ✅ | 번들 저장/조회 | 시끄럽다 |
| **`BSVIBE_GATEWAY_KMS_KEY_B64`** | ❌ **절차로 불가** | 아래 §3 | ❌ **조용하다** |
| `BSVIBE_WORKER_TOKEN` (launchd plist) | ✅ 아래 §2b | 그 워커 한 대 | **시끄럽다** — 401 |

---

## 1. `BSVIBE_APP_DB_PASSWORD` — 런타임 역할 (자가 적용)

**검증 상태: 미검증(메커니즘은 코드로 확인).**

이 비밀은 **두 곳**에 있다 — 그게 이 로테이션의 유일한 함정이다.
* `BSVIBE_APP_DB_PASSWORD` — 마이그레이션이 역할에 **부여**하는 값
* `BSVIBE_DATABASE_URL` — 앱이 **접속**할 때 쓰는 DSN 의 userinfo

둘이 어긋나면 앱이 못 붙는다. `deploy/compose.prod.yaml:56-58` 주석이 그 계약을 적어 뒀다:
*"Must match the userinfo in `BSVIBE_DATABASE_URL`."*

적용은 자동이다. 백엔드가 부팅 때 마이그레이션을 돌리고, `20260715_runtime_role` 이
환경변수가 있으면 `ALTER ROLE bsvibe_app WITH LOGIN PASSWORD %L` 를 실행한다
(트랜잭션-로컬 GUC + `format(%L)` 바인딩 — 문자열 보간이 아니다). 재실행 가능하다.

```bash
# 1. 새 값 생성
NEW=$(openssl rand -base64 32 | tr -d '\n/+=' | head -c 40)

# 2. deploy/.env.prod 에서 BSVIBE_APP_DB_PASSWORD 와
#    BSVIBE_DATABASE_URL 의 userinfo 를 "둘 다" 같은 값으로 바꾼다
#    (한쪽만 바꾸면 부트가 실패한다)

# 3. 백엔드 재생성 → 부팅 시 마이그레이션이 새 비밀번호를 역할에 적용
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod -p bsvibe-prod up -d --force-recreate backend worker

# 4. 검증 — 실행으로, grep 아님
curl -s https://api.bsvibe.dev/api/health      # {"status":"ok",...} 여야 한다
```
⚠️ 순서가 중요하다: 마이그레이션은 **owner DSN**(`BSVIBE_MIGRATION_DATABASE_URL`)으로
돌면서 런타임 역할의 비밀번호를 바꾸고, **그 다음** 앱이 런타임 DSN 으로 붙는다. 그래서
`.env.prod` 의 두 값을 **재시작 전에 함께** 바꿔야 한 번에 끝난다.

---

## 2. `BSVIBE_DB_PASSWORD` — owner 역할 (⚠️ 자가 적용 아님)

**검증 상태: 미검증.**

⚠️ **가장 흔한 오해**: 이 값은 `compose.prod.yaml:19` 에서 postgres 컨테이너의
`POSTGRES_PASSWORD` 로 들어간다. 그런데 `POSTGRES_PASSWORD` 는 **데이터 디렉터리를
처음 만들 때만** 적용된다(공식 이미지 동작). 데이터가 이미 있는 prod 에서는
`.env.prod` 를 바꾸고 컨테이너를 재생성해도 **postgres 안의 비밀번호는 그대로다.**

⇒ `.env.prod` 만 고치면 DSN 과 실제 비밀번호가 어긋나 **부트가 깨진다.**

```bash
# 1. 실제 역할 비밀번호를 먼저 바꾼다 (이것이 진짜 적용 지점)
docker exec -i bsvibe-prod-postgres-1 psql -U bsvibe -d bsvibe \
  -c "ALTER ROLE bsvibe WITH PASSWORD '<NEW>';"

# 2. .env.prod 의 BSVIBE_DB_PASSWORD 와
#    BSVIBE_MIGRATION_DATABASE_URL 의 userinfo 를 둘 다 갱신

# 3. backend/worker 재생성 (postgres 는 건드리지 않는다 — 상태 보유)
# 4. /api/health 로 검증
```

---

## 2b. `BSVIBE_WORKER_TOKEN` — launchd 워커 (2026-09-23 추가)

`.env.prod` 가 아니라 **launchd plist 의 `EnvironmentVariables`** 에 산다.
`~/Library/LaunchAgents/com.bsvibe.worker-<name>.plist`.

> 🚨 **이 절이 생긴 이유.** 2026-09-23 에 에이전트가 plist 인자를 훑다가 그 토큰을
> **자기 도구 출력에 평문으로 찍었다.** #935 가 기록한 2026-09-10 사고와 **같은 모양**이다
> (그때는 `.env.prod` 값 2개, 격리 서브에이전트). 두 번 같은 일이 난 셈이니,
> **시크릿을 들고 있는 파일을 훑을 땐 출력에 리댁션을 걸어라** — `sed -E 's/"[0-9a-f]{32,}"/"<REDACTED>"/g'`.

### 절차

```bash
# 0. 지금 등록된 워커를 확인한다 (id 가 필요하다)
#    MCP: bsvibe_workers_list   /   PWA: 설정 → Workers

# 1. 새 토큰을 발급한다 — 이 호스트에서
bsvibe-worker register --name <name>
#    토큰은 한 번만 보인다. token_urlsafe(32) 이고 서버에는 sha256 만 저장된다
#    (backend/executors/service.py: _hash_token / register_worker_for_workspace)

# 2. plist 의 EnvironmentVariables.BSVIBE_WORKER_TOKEN 을 새 값으로 바꾼다
#    ⚠️ 권한도 같이 좁혀라 — 기본이 -rw-r--r-- 라 로컬 누구나 읽는다
chmod 600 ~/Library/LaunchAgents/com.bsvibe.worker-<name>.plist

# 3. ⚠️ plist 를 고쳤으면 kickstart 로는 안 먹는다
launchctl bootout   gui/501/com.bsvibe.worker-<name>
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.bsvibe.worker-<name>.plist

# 4. 옛 워커 행을 폐기한다
#    MCP: bsvibe_workers_revoke(worker_id=<옛 id>)
```

### 검증 — 양성과 음성을 **둘 다**

| | 무엇을 보나 |
|---|---|
| **양성** | `bsvibe_workers_list` 에서 새 워커의 `heartbeat_fresh: true` + `last_heartbeat` 가 재시작 이후 |
| **양성** | 워커 **프로세스 시작 시각**이 갱신됐다 (`launchctl list` 의 PID 가 바뀐다). 낡음의 유일한 신호다 |
| **음성 대조군** | **옛 토큰으로 직접 쳐서 401 을 본다** — 이게 없으면 "새 토큰이 된다"만 알 뿐 **옛 토큰이 죽었는지는 모른다** |

```bash
# 음성 대조군 — 반드시 하라
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-Worker-Token: <옛 토큰>" https://api.bsvibe.dev/api/v1/workers/heartbeat
# 401 이어야 한다
```

폐기가 실제로 인증을 막는 것은 코드로 확인했다 — `authenticate_worker` 가
`WorkerRow.is_active.is_(True)` 를 WHERE 에 걸고, `revoke_worker` 는 `is_active=False`
로 소프트 삭제하며 Redis 디스패치 스트림도 같이 지운다. **그래도 위 401 을 직접 봐라** —
코드를 읽은 것과 그 경로가 prod 에서 도는 것은 다른 주장이다.

### ⚠️ 이 절이 못 덮는 것

* **`com.bsvibe.worker-admin` 은 위 절차가 그대로 안 맞는다.** 그 plist 는 서버로
  `http://localhost:8700` 을 보는데 그 포트는 **ssh 터널**(`lsof` 실측)이고, prod
  워크스페이스의 `bsvibe_workers_list` 에는 **그 이름의 워커가 없다.**
  ⇒ **어느 백엔드가 그 토큰을 발급했는지 확인하기 전에는 폐기 대상을 특정할 수 없다.**
  터널 반대쪽을 먼저 확인하라
* **낡은 등록이 살아 있다.** 2026-09-23 실측: `dogfood-mac` 이 `is_active: true` 인데
  마지막 하트비트가 **2026-07-20**(두 달 전)이다. 보고를 멈춘 지 두 달 된 머신의 토큰이
  **아직 유효하다.** 로테이션과 별개로 폐기 후보다

---

## 3. ❌ `BSVIBE_GATEWAY_KMS_KEY_B64` — **절차로는 돌릴 수 없다**

**이 절은 절차가 아니라 경고다.**

### 왜 불가능한가
`backend/router/accounts/crypto.py` 의 암호문은 `base64(nonce ‖ ciphertext)` 이고
**키 id 가 없다.** 어떤 암호문이 어느 키로 만들어졌는지 **알 방법이 없다.**
⇒ 이중키 기간(옛 키로 읽고 새 키로 쓰기)이 **원리적으로 불가능**하고, 로테이션은
"전량 재암호화를 한 번에" 뿐이다.

### 그리고 실패가 조용하다 — 이게 진짜 위험
`backend/workflow/domain/verify_secrets.py` 는 복호 실패를 **raise 하지 않고 DROP** 한다.
그 독스트링이 이 상황을 이미 예견하고 그렇게 설계했다:

> *"A value that fails to decrypt is DROPPED rather than raised: the check that needs it
> will fail on its own terms (a login that does not work is a verdict), while a raise here
> would take down every run of a product whose **KMS key rotated**."*

평상시엔 옳은 설계다. **로테이션 중에는 정확히 반대로 작동한다** — 재암호화를 빠뜨린 값이
에러가 아니라 **조용한 부재**가 되고, 그 제품의 검증은 *"로그인이 안 되네"* 로 실패한다.
원인이 로테이션이라는 신호가 **어디에도 없다.**

### 블래스트 반경 (2026-09-14 prod 실측)
| 저장소 | 행 |
|---|---|
| `model_accounts` | 9 |
| `connector_oauth_tokens` | 4 |
| `connector_oauth_app_credentials` | 1 |
| `products.metadata.verify_secrets` | 1 (키 5개) |

복호 호출부는 10개 모듈(`router/accounts` · `runtime/*` · `mcp/tools/connectors_tools` ·
`product_secrets` · `verify_environment`).

### 돌려야 한다면
1. **선행 작업이 먼저다** — 암호문에 key-id 를 실어 이중키 기간을 가능하게 만든다.
   이게 없으면 그 키는 사실상 **영구 고정**이다.
2. 그 전에 불가피하게 돌려야 하면, **재암호화 스크립트 + 다운타임 + 전수 검증**이 한 묶음이다.
   "돌리고 나중에 확인"은 불가능하다 — 실패가 조용하므로 확인할 것이 없다.

---

## 4. 로테이션 후 검증 — 음성 대조군을 반드시 같이

초록만 보면 *"헤더가 온다"* 와 *"내 검사가 틀렸다"* 를 구분할 수 없다.

* **양성**: `curl -s https://api.bsvibe.dev/api/health` → `{"status":"ok",...}`
* **양성**: 실제 런을 하나 돌려 DB 쓰기까지 닿는지 (헬스는 `settings` 만 볼 수 있다 — 얕은
  헬스가 의존성 사망을 초록으로 보증한 전례가 있다, PR #919)
* **음성 대조군**: 일부러 틀린 비밀번호로 붙여 **실패하는 것을 본다.** 이걸 안 보면
  "성공"이 무엇과 대비된 성공인지 모른다

---

## 5. 이 런북이 아직 답하지 못하는 것

* 위 §1·§2 절차는 **한 번도 걸어본 적이 없다**. 걸을 때는 유지보수 창에서, 롤백 경로를
  손에 쥔 채로 하라
* `BSVIBE_OAUTH_PRIVATE_KEY_PEM` 로테이션의 영향 범위(발급된 토큰 전부 무효화)는
  코드로 확인했지만 절차는 쓰지 않았다 — 걸어본 뒤에 적는 것이 맞다
* 백업/복원과의 상호작용: 옛 백업을 복원하면 **그 시점의 키로 암호화된 데이터**가 돌아온다.
  KMS 키를 돌린 뒤 그 전 백업을 복원하면 §3 의 조용한 실패가 그대로 재현된다
