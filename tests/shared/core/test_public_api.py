"""Pin the public re-exports of bsvibe_core.

Migration prompt promises:

    from backend.shared.core import BsvibeSettings, configure_logging, BsvibeError

This test fails the moment one of those breaks — exactly the contract
the 4 product migrations rely on.
"""

from __future__ import annotations


def test_top_level_exports() -> None:
    from backend.shared import core as bsvibe_core

    expected = {
        "BsvibeSettings",
        "configure_logging",
        "BsvibeError",
        "ConfigurationError",
        "ValidationError",
        "NotFoundError",
        "csv_list_field",
        "parse_csv_list",
    }
    missing = expected - set(bsvibe_core.__all__)
    assert not missing, f"missing exports: {missing}"

    for name in expected:
        assert hasattr(bsvibe_core, name), f"bsvibe_core.{name} not importable"


def test_version_attribute_present() -> None:
    from backend.shared import core as bsvibe_core

    assert isinstance(bsvibe_core.__version__, str)
    assert bsvibe_core.__version__.count(".") == 2
