# 런북 — 로컬에서 RLS 를 실제로 검증하기

**왜 이 문서가 있나**: #959 §할 것 4번이 이렇게 적어 뒀다.

> ⚠️ `deploy/compose.yaml:53`(dev)은 **owner 역할**을 쓴다 — **로컬 RLS 검증은 무의미하다.**
> 검증은 CI 또는 prod 에서

맞는 말이지만 **로컬에서 CI 와 동일한 조건을 만들 수 있다.** 2026-09-18 에 실제로 그렇게 해서
CI 에서만 나던 실패를 로컬에서 재현하고 고쳤다. 그 절차를 남긴다.

## 왜 SQLite 로는 절대 안 잡히나

RLS 는 **PostgreSQL 기능**이다. 기본 스위트는 in-memory SQLite 로 도니까 정책이 아예 없다.
게다가 테이블을 `create_all` 로 만들면 **정책은 마이그레이션에 있으므로 PG 를 써도 안 걸린다.**

⇒ 두 조건이 **동시에** 필요하다:
1. `alembic upgrade head` (정책 DDL 이 여기 있다)
2. **`bsvibe_app` 최소권한 역할로 접속** (owner 는 RLS 를 우회한다)

## 절차

```bash
# 1. pgvector 가 있는 PG (평범한 postgres 이미지는 CREATE EXTENSION vector 에서 죽는다)
#    포트는 prod 의 5442 와 겹치지 않게
docker run -d --name pg-rls-check \
  -e POSTGRES_PASSWORD=bsvibe -e POSTGRES_USER=bsvibe -e POSTGRES_DB=bsvibe \
  -p 5459:5432 pgvector/pgvector:pg16
until docker exec pg-rls-check pg_isready -U bsvibe >/dev/null 2>&1; do sleep 2; done

# 2. CI 와 같은 두 DSN. 마이그레이션은 owner 로, 앱은 bsvibe_app 으로.
#    runtime_role 마이그레이션이 BSVIBE_APP_DB_PASSWORD 로 그 역할을 만들어 준다.
export BSVIBE_MIGRATION_DATABASE_URL="postgresql+asyncpg://bsvibe:bsvibe@localhost:5459/bsvibe"
export BSVIBE_DATABASE_URL="postgresql+asyncpg://bsvibe_app:bsvibe_app_ci@localhost:5459/bsvibe"
export BSVIBE_APP_DB_PASSWORD="bsvibe_app_ci"

uv run alembic upgrade head
uv run pytest tests/ -q

# 3. 끝나면
docker rm -f pg-rls-check
```

## ⚠️ 통과했다고 안심하기 전에 — **음성 대조군을 걸어라**

"PG 로 돌렸더니 통과"는 **RLS 가 실제로 동작한 것**과 **설정이 틀려 RLS 가 아예 안 걸린 것**을
구분하지 못한다. 둘 다 초록이다.

⇒ **고치기 전 코드로 한 번 돌려서 빨개지는 것을 봐라.** 2026-09-18 에 이렇게 확인했다:

| | `tests/notifications/test_daily_brief_worker.py` |
|---|---|
| 수정 전(plain `workspace_scope`) | ❌ `assert '0 shipped …' == '1 shipped …'` — **CI 실패를 그대로 재현** |
| 수정 후(`workspace_session_scope`) | ✅ 통과 |

이 대조군이 없으면 "내 하네스가 RLS 를 안 켰다"를 "고쳤다"로 읽게 된다.

## 숫자로 본 차이

| | SQLite (기본) | PG + RLS |
|---|---|---|
| 통과 | 6,383 | **6,431** |
| 스킵 | 49 | **1** |

**48개가 더 돈다.** 그 48개가 RLS·advisory lock·`SKIP LOCKED` 같은 PG 전용 프리미티브를
재는 테스트들이고, 로컬 기본 실행에서는 **전부 스킵된다**.

## 이 절차가 실제로 잡은 것 (2026-09-18)

`after_begin` 리스너는 GUC 를 **트랜잭션당 한 번** 쓴다. 배경 루프가 세션을 루프 **바깥**에서
열면, 반복마다 contextvar 를 바꿔도 GUC 는 첫 워크스페이스로 armed 된 채 남는다.

RLS 정책은 비대칭이다 — 빈 GUC 는 **fail-open**, **다른** 워크스페이스의 GUC 는 **fail-closed**.
그래서 두 번째 워크스페이스의 질의가 **에러가 아니라 0행**을 돌려준다. 조용한 오답이다.

⇒ 세션을 공유하는 루프는 `backend.data.rls.workspace_session_scope` 를 써라
(두 층 다 publish 하고 나갈 때 GUC 를 비운다). 반복마다 자기 트랜잭션을 여는 루프는
`workspace_scope` 로 충분하다 — `after_begin` 이 스코프 안에서 제대로 armed 된다.
