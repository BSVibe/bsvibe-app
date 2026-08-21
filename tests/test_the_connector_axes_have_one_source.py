"""감사 C4 · C11 — 커넥터의 손-미러 축 둘을 없앤다.

## C4 — 시크릿 마스킹 목록이 두 벌이고, **이미 한 번 샜다**

``mcp/tools/connectors_tools.py`` 가 자백해뒀다:

    *"Mirrors the REST ``_SECRET_DELIVERY_KEYS`` in ``backend/api/v1/connectors.py``
    so MCP + PWA are consistent (**previously the MCP serializer echoed these
    unredacted**)."*

미러라서 한 번 어긋났고, 어긋난 결과가 **응답에 라이브 크리덴셜 노출**이었다.
그리고 **지금도 어긋나 있다** — REST 응답은 ``needs_reauth`` 를 담는데 MCP 는
그 단어가 **0회**다. 재연결이 필요한 커넥터를 MCP 사용자는 볼 수 없다.

## C11 — 인터랙티브 승인 능력이 INV-1 카탈로그 **밖**에서 손유지된다

``ConnectorInfo`` docstring: *"Every field is derived from ``PluginMeta`` + the
webhook registry — **there is no hand-maintained second copy**."*
그런데 "이 커넥터가 버튼 탭 승인을 받을 수 있나"는 ``api/webhooks.py`` 의
``_INTERACTION_CALLBACKS`` 딕셔너리에만 있다 — 콜백 구현은 전부
``backend/connectors/`` 에 사는데 **등록부만 api 층에 있었다.**

등록부를 소유 컨텍스트로 옮기고 카탈로그가 그것에서 **파생**하게 한다.
지연 import 규율(R2c — ``api.webhooks`` 는 ``plugin.*`` 정적 엣지 0)은 그대로 지킨다.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"


# ── C4 — 구조 가드 ────────────────────────────────────────────────────────


def test_the_secret_key_set_is_declared_once() -> None:
    """시크릿 키 목록이 트리 전체에 **한 곳**이어야 한다 — 미러가 이미 한 번 샜다."""
    sites = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _BACKEND.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "SECRET_DELIVERY_KEYS = frozenset" in line
    ]
    assert len(sites) == 1, f"시크릿 키 목록이 여러 곳에 있다: {sites}"


def test_both_surfaces_share_the_redaction_helpers() -> None:
    """특성화 — REST 와 MCP 가 **같은 함수 객체**를 쓴다."""
    from backend.api.v1 import connectors as rest
    from backend.common.connector_redaction import public_delivery_config, token_hint
    from backend.mcp.tools import connectors_tools as mcp

    assert rest._public_delivery_config is public_delivery_config  # noqa: SLF001
    assert mcp._public_delivery_config is public_delivery_config  # noqa: SLF001
    assert rest._token_hint is token_hint  # noqa: SLF001
    assert mcp._token_hint is token_hint  # noqa: SLF001


def test_redaction_drops_every_secret_bearing_key() -> None:
    """특성화 — 통합 후에도 세 키가 전부 빠져야 한다."""
    from backend.common.connector_redaction import public_delivery_config

    cfg = {
        "webhook_secret": "s1",
        "signing_secret": "s2",
        "client_secret": "s3",
        "parent_page_id": "keep-me",
    }
    assert public_delivery_config(cfg) == {"parent_page_id": "keep-me"}


def test_token_hint_reveals_only_the_last_four() -> None:
    from backend.common.connector_redaction import token_hint

    assert token_hint("abcdefghijkl") == "...ijkl"


def test_the_mcp_connector_row_carries_needs_reauth() -> None:
    """**드리프트 수정** — REST 응답에는 있고 MCP 에는 없던 필드."""
    import inspect

    from backend.mcp.tools import connectors_tools as mcp

    src = inspect.getsource(mcp)
    assert '"needs_reauth"' in src, "MCP 커넥터 목록이 재연결 필요를 알려주지 않는다"


# ── C11 — 구조 가드 ───────────────────────────────────────────────────────


def test_the_interaction_registry_lives_in_the_owning_context() -> None:
    """등록부는 콜백 구현과 같은 컨텍스트에 있어야 한다 — api 층이 아니라."""
    from backend.connectors.interactions import INTERACTION_CONNECTORS

    assert INTERACTION_CONNECTORS == frozenset({"telegram", "slack", "discord"})

    webhooks = (_BACKEND / "api" / "webhooks.py").read_text(encoding="utf-8")
    assert "_INTERACTION_CALLBACKS: dict" not in webhooks, "api 층에 등록부가 남아 있다"


def test_the_catalog_derives_the_interactive_approval_flag() -> None:
    """INV-1 — 네 번째 축이 손유지가 아니라 **파생**이어야 한다."""
    from backend.connectors.catalog import get_connector_catalog
    from backend.connectors.interactions import INTERACTION_CONNECTORS

    catalog = get_connector_catalog()
    for name, info in catalog.items():
        assert info.interactive_approval == (name in INTERACTION_CONNECTORS), name


def test_the_interaction_callback_import_stays_lazy() -> None:
    """R2c 규율 — ``api.webhooks`` 는 ``plugin.*`` 정적 엣지가 0이어야 한다.

    등록부를 옮겨도 그 규율은 유지된다: 콜백 모듈 import 는 호출 시점에 일어난다."""
    interactions = (_BACKEND / "connectors" / "interactions.py").read_text(encoding="utf-8")
    top_level = [
        line
        for line in interactions.splitlines()
        if line.startswith(("import ", "from ")) and "_callback" in line
    ]
    assert not top_level, f"콜백 모듈을 최상위에서 import 한다: {top_level}"


def test_the_three_capability_flags_still_derive_from_plugins() -> None:
    """양성 대조군 — 기존 세 축이 그대로여야 한다."""
    from backend.connectors.catalog import get_connector_catalog

    catalog = get_connector_catalog()
    assert catalog, "카탈로그가 비었다"
    for info in catalog.values():
        for field in ("outbound", "importable", "webhook_trigger", "user_connectable"):
            assert isinstance(getattr(info, field), bool)
