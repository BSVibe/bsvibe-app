"""Run-scoped access tokens — what a dispatched executor task carries (T2).

The executor is the user's LLM client. To act on a run it calls BSVibe's tools over MCP, and
to do that it needs a token. That token must be the narrowest thing that works:

* it names ONE run (``run_id`` claim → :attr:`McpPrincipal.run_id`), so a leaked worker token
  reaches one run's worktree and nothing else;
* it is short-lived — the task's own lifetime, not the founder's session;
* it is NOT the founder's workspace token: an ordinary MCP token has no ``run_id`` and the
  work tools refuse it outright (T1).

The claim rides through the same verification the rest of the MCP surface uses (signature,
revocation row, expiry), so nothing here weakens the existing path.
"""

from __future__ import annotations

import time
import uuid

import pytest

from backend.identity.oauth_jwt import issue_access_token, verify_access_token

pytestmark = pytest.mark.asyncio


def _issue(*, run_id: uuid.UUID | None) -> str:
    now = int(time.time())
    return issue_access_token(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="bsvibe-worker",
        scope=["mcp:read", "mcp:write"],
        jti=uuid.uuid4(),
        issued_at=now,
        expires_at=now + 900,
        issuer="https://api.bsvibe.dev",
        run_id=run_id,
    )


async def test_the_run_rides_in_the_token() -> None:
    run_id = uuid.uuid4()

    claims = verify_access_token(_issue(run_id=run_id), issuer="https://api.bsvibe.dev")

    assert claims["run_id"] == str(run_id)


async def test_an_ordinary_token_carries_no_run() -> None:
    """The founder's editor token must never be able to edit code inside a run: the work tools
    key off the absence of this claim."""
    claims = verify_access_token(_issue(run_id=None), issuer="https://api.bsvibe.dev")

    assert "run_id" not in claims
