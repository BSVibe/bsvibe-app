"""glue 테스트 공용 fixture."""

import pytest


@pytest.fixture(autouse=True)
def _public_dns_for_connector_egress(monkeypatch):
    """The e2e connector tests use fake ``*.test`` API hosts behind a mock transport."""

    async def _public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr("backend.extensions.plugin.egress.resolve_host", _public)
