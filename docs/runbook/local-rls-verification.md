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

---

## 블라인드 접근을 **런타임으로** 세기 (2026-09-28, #959)

*"강제 표를 GUC 없이 읽는 곳이 어디냐"* 를 grep 으로 세면 내가 아는 곳만 나온다. 09-28 에 인수인계가
*"requests 하나뿐"* 이라 적었고 그 전제로 결정까지 받았는데, 런타임으로 세니 **11곳**이었다.

### 1. 프로브 — 빈 GUC 로 평가된 질의를 기록하고, 판정은 fail-open 그대로

판정을 안 바꾸니 픽스처가 안 죽는다(fail-closed 로 돌리면 110건 중 108건이 **픽스처 INSERT** 에서 죽어서
제품 코드까지 가지도 못한다). 위 절차로 PG 를 올린 뒤 owner 로:

```sql
-- 빈 GUC 로 강제 표를 건드린 질의를 **서버 로그**에 남긴다. 판정은 fail-open 그대로(항상 true).
CREATE OR REPLACE FUNCTION rls_probe(t text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER AS $$
BEGIN
  IF coalesce(current_setting('app.current_workspace_id', true), '') = '' THEN
    RAISE LOG 'RLSPROBE|%|%', t, left(regexp_replace(current_query(), '\s+', ' ', 'g'), 600);
  END IF;
  RETURN true;
END $$;
```

정책 6개를 `USING (rls_probe('<t>') AND <기존식>) WITH CHECK (rls_probe('<t>') AND <기존식>)` 로 다시 만든다.
런 시작 시각을 적어 두고 끝나면 `docker logs --since <T0> pg-rls-check 2>&1 | grep -o 'RLSPROBE|.*'` 로 뽑는다.

🚨 **로그를 표에 INSERT 하지 마라 — 롤백이 기록을 지운다** (09-29). 09-28 판은 `rls_probe_log` 표에
INSERT 했다. 그 INSERT 는 **측정 대상과 같은 트랜잭션**이라, 읽기만 하고 끝나는 세션(SQLAlchemy 는 닫을 때
rollback)의 블라인드 기록이 통째로 사라졌다 — 전체 스위트에서 **약 920건**(6337 → 7260). 09-28 의 「11곳」도
이 장치로 센 것이다. 드러난 경위: production 계층 블라인드 **0**이 나왔는데, 반드시 찍혀야 할 테스트 헬퍼의
블라인드 조회까지 0 이었다 — **양성 대조군을 롤백되는 세션에 걸어라**(아래 대조군).

⚠️ 그 전 판(`ON CONFLICT DO UPDATE` 카운터)은 두 세션이 **같은 로그 행 락**을 다퉈 테스트를 교착시켰다
(09-28, 4시간). `RAISE LOG` 는 락도 트랜잭션도 없다. 긴 런에는 그래도 **출력 정지 감시**를 걸어라.

⚠️ 정책식은 **행마다** 평가된다 — 빈 표에 대한 블라인드 SELECT 는 프로브를 부르지 않는다. 대조군은 행을 하나 넣고.

### 2. 발행 지점 태그 — SQL 주석으로 파이썬 호출 지점을 DB 까지 실어 보낸다

`current_query()` 는 주석까지 돌려준다. 측정 전용 pytest 플러그인(레포에 넣지 않는다):

```python
"""측정 전용 pytest 플러그인 — 모든 SQL 에 발행 지점 주석을 붙인다.

주석: /*site:<첫 backend/ 프레임>|test:<첫 tests/ 프레임>*/
DB 쪽 rls_probe 가 current_query() 로 이 주석까지 기록한다.
"""

from __future__ import annotations

import sys

from sqlalchemy import event
from sqlalchemy.engine import Engine

_ROOT = "/wt/959-claim/"


def _frames():  # type: ignore[no-untyped-def]
    import greenlet

    f = sys._getframe(3)
    g = greenlet.getcurrent()
    while True:
        while f is not None:
            yield f
            f = f.f_back
        g = g.parent
        if g is None:
            return
        f = g.gr_frame


def _site() -> str:
    backend = test = entry = "-"
    for f in _frames():
        fn = f.f_code.co_filename
        if _ROOT in fn:
            rel = fn.split(_ROOT, 1)[1]
            if backend == "-" and rel.startswith("backend/") and "/data/" not in rel:
                backend = f"{rel}:{f.f_lineno}:{f.f_code.co_name}"
            if rel.startswith("backend/"):
                entry = f"{rel}:{f.f_code.co_name}"
            if test == "-" and rel.startswith("tests/"):
                test = f"{rel}:{f.f_code.co_name}"
                break
    return f"/*site:{backend}|entry:{entry}|test:{test}*/ "


@event.listens_for(Engine, "before_cursor_execute", retval=True)
def _tag(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
    return _site() + statement, parameters
```

`PYTHONPATH=<dir> uv run pytest -p sitetag_plugin ...`. `_ROOT` 는 워크트리 경로에 맞춰라.
⚠️ **그린렛 경계**: SQLAlchemy async 는 SQL 을 별도 그린렛에서 돌려 프레임 체인이 끊긴다 —
`gr_frame` 으로 부모 그린렛을 건너지 않으면 전부 `site:-` 로 찍힌다(양성 대조군 한 테스트로 먼저 확인).

### 3. 진입점으로 묶어라 — 제품 블라인드와 테스트 블라인드를 가른다

테스트가 서비스 함수를 **직접** 부르면 prod 에선 라우트가 세워 줄 스코프를 건너뛴다 ⇒ 테스트가 만든 블라인드.
prod 에서 실제로 블라인드인 건 **배경 워커 진입점**을 거친 것뿐이다.

```python
import collections, re, sys
c = collections.Counter()
for line in open(sys.argv[1]):  # docker logs 에서 뽑은 RLSPROBE 줄
    m = re.match(r"RLSPROBE\|(\w+)\|/\*site:([^|]*)\|entry:([^|]*)\|test:([^*]*)\*/ (\w+)", line)
    if m and m[2].startswith(("backend/", "plugin/")):
        c[m[3].split(":")[0]] += 1
for entry, n in c.most_common():
    print(n, entry)
```

**API·MCP 진입점의 블라인드는 대부분 테스트가 만든 것이다** (09-29 판정): API 테스트는 `get_workspace_id` 를
override 하고, MCP 테스트는 `registry.call_tool` 을 직접 불러 GUC 를 세우는 `mcp/server.py` 를 건너뛴다.
판별자는 **`tests/production/*`** — 실제 인증 경로를 override 없이 돈다. 거기서 제품 코드 블라인드가 0 이면
API/MCP 는 닫힌 것이다. prod 에서도 블라인드인 건 **사용자 세션이 없는 경로**다: 배경 워커 · 웹훅 · 운영 CLI.

### 대조군 (둘 다 없으면 숫자를 쓰지 마라)

* **장치**: 런 **전후**로 정책에 프로브가 6/6 붙어 있는지 센다 — `tests/data/test_rls_pg.py` 는
  **스키마를 DROP 하고 마이그레이션을 다시 돌려** 정책을 원복시킨다. 측정 코퍼스에서 빼라
* **프로브**: bsvibe_app 으로 **`BEGIN; <블라인드 SELECT>; ROLLBACK;`** 은 기록되고, GUC 를 세운 SELECT 는
  안 기록되는지. 롤백 대조군이 없으면 09-28 의 결함(롤백이 기록을 지움)을 못 잡는다
* **코퍼스 안 양성 대조군**: 결과 집합에 **반드시 있어야 할** 블라인드 하나를 먼저 찾아라
  (09-29: `test_queue_claims_cross_tenant._statuses`). 그게 0 이면 숫자 전체를 버려라
* ⚠️ fail-closed 변환을 손으로 쓸 때 **GUC 이름**을 틀리기 쉽다(`app.current_workspace_id`). 틀려도 `''` 부재만
  세는 카운터는 6/6 을 준다 — 카운터에 `qual LIKE '%app.current_workspace_id%' AND with_check = qual` 까지 넣어라
