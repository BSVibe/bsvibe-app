"""The public bsvibe_fastapi import surface.

Pinned so consumers (the four products) import a stable set of names.
"""

from __future__ import annotations


def test_public_api() -> None:
    from backend.shared import fastapi as bsvibe_fastapi

    expected = {
        "FastApiSettings",
        "add_cors_middleware",
        "make_health_router",
        "RequestIdMiddleware",
        "__version__",
    }
    assert expected.issubset(set(bsvibe_fastapi.__all__))
    for name in expected:
        assert hasattr(bsvibe_fastapi, name), f"missing public name: {name}"


def test_version_string_is_set() -> None:
    from backend.shared import fastapi as bsvibe_fastapi

    assert isinstance(bsvibe_fastapi.__version__, str)
    assert bsvibe_fastapi.__version__
