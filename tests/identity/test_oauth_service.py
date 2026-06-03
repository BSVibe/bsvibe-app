"""Tests for backend.identity.oauth_service — Lift D1.

Drives the OAuth state machine on an in-memory SQLite session: code
issuance → claim, token mint + rotation + revocation + introspection.
The PKCE / JWT lower layers are unit-tested separately; here we cover
the persistence + transitions.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.identity.db import UserRow
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.oauth_service import (
    CodeClaimOutcome,
    RefreshRotateOutcome,
    RevokeKind,
    claim_authorization_code,
    introspect_token,
    issue_authorization_code,
    issue_token_pair,
    revoke_token,
    rotate_refresh_token,
)
from backend.identity.workspaces_db import WorkspaceRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio

ISSUER = "http://test/oauth"


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


VERIFIER = "abcDEF123-._~abcDEF123-._~abcDEF123-._~xyzAB"
CHALLENGE = _challenge_for(VERIFIER)


@pytest.fixture(autouse=True)
def _reset_keys() -> None:
    reset_signing_key_for_tests()
    yield
    reset_signing_key_for_tests()


async def _seed_user_workspace(session) -> tuple[UserRow, WorkspaceRow]:
    ws = WorkspaceRow(name="t-ws", region="us-1")
    session.add(ws)
    user = UserRow(supabase_user_id=f"sb-{uuid.uuid4()}", email="t@example.com")
    session.add(user)
    await session.flush()
    return user, ws


async def test_issue_then_claim_authorization_code_happy_path() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        code = await issue_authorization_code(
            s,
            client_id="dcr-x",
            user_id=user.id,
            workspace_id=ws.id,
            scope=["mcp:read"],
            redirect_uri="http://127.0.0.1:54321/callback",
            code_challenge=CHALLENGE,
        )
        outcome, claimed = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1:54321/callback",
            code_verifier=VERIFIER,
        )
        assert outcome is CodeClaimOutcome.CLAIMED
        assert claimed is not None
        assert claimed.user_id == user.id
        assert claimed.workspace_id == ws.id
        assert claimed.scope == ["mcp:read"]


async def test_claim_unknown_code() -> None:
    async with memory_session() as s:
        outcome, _ = await claim_authorization_code(
            s,
            code="never-issued",
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1/cb",
            code_verifier=VERIFIER,
        )
        assert outcome is CodeClaimOutcome.NOT_FOUND


async def test_claim_replay_returns_used() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        code = await issue_authorization_code(
            s,
            client_id="dcr-x",
            user_id=user.id,
            workspace_id=ws.id,
            scope=["mcp:read"],
            redirect_uri="http://127.0.0.1:54321/callback",
            code_challenge=CHALLENGE,
        )
        await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1:54321/callback",
            code_verifier=VERIFIER,
        )
        outcome, _ = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1:54321/callback",
            code_verifier=VERIFIER,
        )
        assert outcome is CodeClaimOutcome.USED


async def test_claim_wrong_client_burns_code() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        code = await issue_authorization_code(
            s,
            client_id="dcr-x",
            user_id=user.id,
            workspace_id=ws.id,
            scope=["mcp:read"],
            redirect_uri="http://127.0.0.1/cb",
            code_challenge=CHALLENGE,
        )
        outcome, _ = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="evil-other-client",
            redirect_uri="http://127.0.0.1/cb",
            code_verifier=VERIFIER,
        )
        assert outcome is CodeClaimOutcome.CLIENT_MISMATCH
        # And a legitimate retry now fails with USED — the row IS consumed.
        outcome2, _ = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1/cb",
            code_verifier=VERIFIER,
        )
        assert outcome2 is CodeClaimOutcome.USED


async def test_claim_bad_pkce_verifier() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        code = await issue_authorization_code(
            s,
            client_id="dcr-x",
            user_id=user.id,
            workspace_id=ws.id,
            scope=["mcp:read"],
            redirect_uri="http://127.0.0.1/cb",
            code_challenge=CHALLENGE,
        )
        outcome, _ = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1/cb",
            code_verifier="A" * 43,  # wrong
        )
        assert outcome is CodeClaimOutcome.PKCE_MISMATCH


async def test_claim_redirect_uri_mismatch_in_token_step() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        code = await issue_authorization_code(
            s,
            client_id="dcr-x",
            user_id=user.id,
            workspace_id=ws.id,
            scope=["mcp:read"],
            redirect_uri="http://127.0.0.1:54321/cb",
            code_challenge=CHALLENGE,
        )
        outcome, _ = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1:54322/cb",  # different port
            code_verifier=VERIFIER,
        )
        assert outcome is CodeClaimOutcome.REDIRECT_URI_MISMATCH


async def test_expired_code_rejected() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        past = datetime.now(UTC) - timedelta(hours=1)
        code = await issue_authorization_code(
            s,
            client_id="dcr-x",
            user_id=user.id,
            workspace_id=ws.id,
            scope=["mcp:read"],
            redirect_uri="http://127.0.0.1/cb",
            code_challenge=CHALLENGE,
            now=past,
        )
        outcome, _ = await claim_authorization_code(
            s,
            code=code,
            expected_client_id="dcr-x",
            redirect_uri="http://127.0.0.1/cb",
            code_verifier=VERIFIER,
        )
        assert outcome is CodeClaimOutcome.EXPIRED


async def test_issue_token_pair_yields_valid_jwt() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        assert pair.access_token.count(".") == 2
        assert pair.refresh_token
        assert pair.expires_in == 3600


async def test_introspect_active_access_token() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        result = await introspect_token(s, token=pair.access_token, issuer=ISSUER)
        assert result.active is True
        assert result.sub == str(user.id)
        assert result.workspace_id == str(ws.id)
        assert result.client_id == "dcr-x"
        assert result.scope == "mcp:read"
        assert result.token_type == "access_token"


async def test_introspect_active_refresh_token() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        result = await introspect_token(
            s,
            token=pair.refresh_token,
            issuer=ISSUER,
            hint=RevokeKind.REFRESH_TOKEN,
        )
        assert result.active is True
        assert result.token_type == "refresh_token"


async def test_introspect_garbage_inactive() -> None:
    async with memory_session() as s:
        result = await introspect_token(s, token="not-a-token", issuer=ISSUER)
        assert result.active is False


async def test_revoke_access_token() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        assert await revoke_token(s, token=pair.access_token, issuer=ISSUER) is True
        result = await introspect_token(s, token=pair.access_token, issuer=ISSUER)
        assert result.active is False


async def test_revoke_refresh_token_cascades_to_access() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        ok = await revoke_token(
            s,
            token=pair.refresh_token,
            issuer=ISSUER,
            hint=RevokeKind.REFRESH_TOKEN,
        )
        assert ok is True
        # Access introspection now inactive
        result = await introspect_token(s, token=pair.access_token, issuer=ISSUER)
        assert result.active is False


async def test_revoke_unknown_token_returns_false_but_no_error() -> None:
    async with memory_session() as s:
        assert await revoke_token(s, token="nope", issuer=ISSUER) is False


async def test_rotate_refresh_token_happy() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        outcome, new_pair = await rotate_refresh_token(
            s,
            refresh_token=pair.refresh_token,
            client_id="dcr-x",
            issuer=ISSUER,
        )
        assert outcome is RefreshRotateOutcome.ROTATED
        assert new_pair is not None
        assert new_pair.access_token != pair.access_token
        assert new_pair.refresh_token != pair.refresh_token


async def test_rotate_refresh_token_reuse_detected() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        await rotate_refresh_token(
            s,
            refresh_token=pair.refresh_token,
            client_id="dcr-x",
            issuer=ISSUER,
        )
        outcome, _ = await rotate_refresh_token(
            s,
            refresh_token=pair.refresh_token,
            client_id="dcr-x",
            issuer=ISSUER,
        )
        assert outcome is RefreshRotateOutcome.REUSED


async def test_rotate_refresh_invalid_client() -> None:
    async with memory_session() as s:
        user, ws = await _seed_user_workspace(s)
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-x",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        outcome, _ = await rotate_refresh_token(
            s,
            refresh_token=pair.refresh_token,
            client_id="evil-other",
            issuer=ISSUER,
        )
        assert outcome is RefreshRotateOutcome.INVALID


async def test_rotate_refresh_unknown() -> None:
    async with memory_session() as s:
        outcome, _ = await rotate_refresh_token(
            s,
            refresh_token="nope-not-a-token",
            client_id="dcr-x",
            issuer=ISSUER,
        )
        assert outcome is RefreshRotateOutcome.INVALID
