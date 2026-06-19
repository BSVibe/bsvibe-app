"""Worker boot registers DB-stored connector OAuth providers (issue #362).

`load_app_credential_providers` (GitHub App Manifest flow → registered OAuth
providers) runs in the API lifespan but historically NOT in the worker. Without
it, `get_provider("github")` is None in the worker process, so
`resolve_connector_credentials` silently skips token refresh — an expired github
push token then fails the `deliver_github` push and no PR opens. The worker boot
must register the same providers (soft-fail, mirroring the API).
"""

from __future__ import annotations

from backend.workflow.application.runtime import lifecycle

# A valid 32-byte key (decoded form, as `_key_from_settings` returns) so the
# lazy CredentialCipher construction in the helper doesn't raise in the test
# env (no KMS key set).
_TEST_KEY = b"x" * 32


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _fake_factory() -> _FakeSession:
    return _FakeSession()


async def test_worker_bootstrap_registers_oauth_providers(monkeypatch) -> None:
    calls: list[object] = []

    async def _spy(session, cipher):  # noqa: ANN001
        calls.append((session, cipher))

    monkeypatch.setattr("backend.connectors.auth.bootstrap.load_app_credential_providers", _spy)
    monkeypatch.setattr("backend.router.accounts.crypto._key_from_settings", lambda: _TEST_KEY)
    await lifecycle._bootstrap_db_oauth_providers(_fake_factory)  # noqa: SLF001
    assert len(calls) == 1, "worker boot must load DB OAuth App-credential providers"


async def test_worker_bootstrap_soft_fails(monkeypatch) -> None:
    async def _boom(session, cipher):  # noqa: ANN001
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("backend.connectors.auth.bootstrap.load_app_credential_providers", _boom)
    # Must NOT raise — a provider-load hiccup never blocks worker boot.
    await lifecycle._bootstrap_db_oauth_providers(_fake_factory)  # noqa: SLF001
