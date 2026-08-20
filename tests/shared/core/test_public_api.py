"""Pin the public re-exports of ``backend.shared.core``.

The surface this used to pin was *"the contract the 4 product migrations rely
on"* — and consolidation onto a single product removed those 4. What is pinned
now is the half production actually calls; the rest was deleted 2026-08-20.
"""

from __future__ import annotations


def test_top_level_exports() -> None:
    from backend.shared import core as bsvibe_core

    expected = {
        "configure_logging",
        "csv_list_field",
        "parse_csv_list",
        "redact_url_password",
    }
    assert set(bsvibe_core.__all__) == expected
    for name in expected:
        assert hasattr(bsvibe_core, name), f"bsvibe_core.{name} not importable"


def test_version_attribute_present() -> None:
    from backend.shared import core as bsvibe_core

    assert isinstance(bsvibe_core.__version__, str)
    assert bsvibe_core.__version__.count(".") == 2
