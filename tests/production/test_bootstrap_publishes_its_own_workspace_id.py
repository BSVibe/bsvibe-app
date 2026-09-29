"""워크스페이스 생성이 **자기 id 를 GUC 로 게시하고** 쓰는가 — #959 의 차단기.

RLS 정책은 GUC 가 비어 있으면 통과시켰다(fail-open, #959 ③ 에서 닫힘):

```sql
USING ( GUC IS NULL OR GUC = '' OR id::text = GUC )
```

#959 가 *"fail-open 은 영구적이다"* 라고 적은 것이 이 탈출구다. 그런데 그걸 닫으려
하면 **워크스페이스를 만들 수 없게 된다**: 아직 없는 워크스페이스라 걸 GUC 가 없고
`WITH CHECK (id::text = GUC)` 가 INSERT 를 거절한다 — 닭-달걀.

2026-09-28 실측(일회용 PG, CI 와 같은 두 역할): 정책에서 빈-GUC 탈출구만 제거하니
`tests/production/`+`tests/data/` 가 27 passed → **4 failed**, 워커 쪽 부분집합이
1034 passed → **20 failed** 였다. 깨진 것은 전부 **GUC 없이 강제 표에 INSERT**
였고(`execution_runs` 48 · `requests` 24 · `workspaces` 4) **배경 루프는 하나도
없었다.** 제품 코드의 차단기는 워크스페이스 생성 하나였다.

해법은 정책을 약화시키는 게 아니다. **id 는 앱이 만든다** — 쓰기 전에 그 id 를
게시하면 `WITH CHECK` 가 통과한다.

## 왜 정책을 바꾸지 않고 재는가

첫 판은 이 테스트 안에서 정책을 fail-closed 로 조였다. 그게 **다른 테스트를 간헐적으로
깨뜨렸다** — `DROP/CREATE POLICY` 는 ACCESS EXCLUSIVE 락이고, 공유 DB 런 한가운데서
치면 옆 테스트와 부딪힌다. 한 번은 2건 실패, 재실행하니 통과 ⇒ 내가 만든 flake 다.

같은 명제를 **DDL 없이** 잴 수 있다: GUC 에 *남의* 워크스페이스 id(비어 있지 않은
값)를 박아 두면 오늘 정책으로도 `id ≠ GUC` 라 INSERT 가 거절된다. 그 상태에서 진짜
부트스트랩이 성공한다면, 그건 **부트스트랩이 자기 id 를 게시했다**는 뜻이다.
fail-closed 가 요구하는 성질과 정확히 같은 것이고, 락도 안 잡는다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.service import ensure_user_bootstrapped

from .conftest import requires_real_pg

pytestmark = requires_real_pg

_GUC = "app.current_workspace_id"


async def _poison_guc(session, foreign_id: uuid.UUID) -> None:
    """이 세션의 트랜잭션에 **남의** 워크스페이스 id 를 건다(`is_local=true`).

    fail-open 탈출구는 *비어 있음* 에만 열렸다 — 비어 있지 않은 값은 그때도
    `id::text = GUC` 를 강제했다. 즉 이 한 줄이 fail-closed 를 흉내 냈다(③ 이후엔
    빈 값도 같은 결과다).
    """
    conn = await session.connection()
    await conn.execute(text(f"SELECT set_config('{_GUC}', :v, true)"), {"v": str(foreign_id)})


async def test_a_foreign_guc_really_blocks_a_workspace_insert(
    session_factory: async_sessionmaker,
) -> None:
    """양성 대조군 — 이게 없으면 아래 초록이 *"정책이 안 물었다"* 일 수 있다."""
    async with session_factory() as session:
        await _poison_guc(session, uuid.uuid4())
        conn = await session.connection()
        with pytest.raises(Exception, match="row-level security"):
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, safe_mode, legal_basis, language, "
                    "timezone, verify_stack_slots, created_at, updated_at) VALUES "
                    "(:i, 'blocked', true, 'contract', 'en', 'UTC', 1, now(), now())"
                ),
                {"i": uuid.uuid4()},
            )


async def test_bootstrap_creates_its_workspace_even_under_a_foreign_guc(
    session_factory: async_sessionmaker,
) -> None:
    """진짜 부트스트랩은 남의 GUC 아래에서도 자기 워크스페이스를 만든다.

    위 대조군이 같은 조건에서 막히므로, 여기서의 성공은 **부트스트랩이 쓰기 전에
    자기 id 를 게시했다**는 것 말고는 설명이 없다.
    """
    async with session_factory() as session:
        await _poison_guc(session, uuid.uuid4())
        _user, membership = await ensure_user_bootstrapped(
            session,
            supabase_user_id="supa-foreign-guc",
            email="foreign-guc@example.com",
        )

    assert isinstance(membership.workspace_id, uuid.UUID)

    # 그리고 그 행이 실제로 DB 에 있다 — 게시한 스코프로 읽으면 보인다.
    async with session_factory() as session:
        conn = await session.connection()
        await conn.execute(
            text(f"SELECT set_config('{_GUC}', :v, true)"), {"v": str(membership.workspace_id)}
        )
        found = (
            await conn.execute(
                text("SELECT count(*) FROM workspaces WHERE id = :i"),
                {"i": membership.workspace_id},
            )
        ).scalar_one()
    assert found == 1
