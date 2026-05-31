"""Tests for the @p.compensate capability + outbound compensation metadata.

Workflow §9 (Direct-output compensation). A plugin pairs an ``@p.outbound``
(which declares ``compensation_tier`` / ``compensation_supported``) with an
``@p.compensate`` handler for tiers T1-T3. The runner dispatches compensation
by artifact_type, mirroring outbound dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.extensions.plugin import (
    VALID_COMPENSATION_TIERS,
    CompensateCapability,
    PluginRegistrationError,
    PluginRunError,
    PluginRunner,
    plugin,
)


class _Ctx:
    def __init__(self, **kwargs: Any) -> None:
        self.credentials: dict[str, Any] = kwargs.get("credentials", {})


class TestOutboundCompensationMetadata:
    def test_outbound_records_tier_and_supported(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")

        @p.outbound(
            artifact_types=["pr"],
            compensation_tier="t2_trail",
            compensation_supported=True,
        )
        async def deliver_pr(context, event):
            return {"external_ref": "github://o/r/pull/1"}

        cap = p.meta.outbounds[0]
        assert cap.compensation_tier == "t2_trail"
        assert cap.compensation_supported is True

    def test_outbound_defaults_no_compensation(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")

        @p.outbound(artifact_types=["pr"])
        async def deliver_pr(context, event):
            return {}

        cap = p.meta.outbounds[0]
        assert cap.compensation_tier is None
        assert cap.compensation_supported is False

    def test_outbound_rejects_invalid_tier(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")
        with pytest.raises(PluginRegistrationError, match="compensation_tier"):

            @p.outbound(artifact_types=["pr"], compensation_tier="t9_magic")
            async def deliver_pr(context, event):
                return {}

    def test_valid_tiers_frozenset(self):
        assert VALID_COMPENSATION_TIERS == frozenset(
            {"t1_clean", "t2_trail", "t3_new_artifact", "t4_irreversible"}
        )


class TestCompensateRegistration:
    def test_registers_compensate_capability(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")

        @p.compensate(artifact_types=["pr"])
        async def revert_pr(context, handle):
            return {"status": "partially_compensated"}

        assert len(p.meta.compensates) == 1
        cap = p.meta.compensates[0]
        assert isinstance(cap, CompensateCapability)
        assert cap.artifact_types == ("pr",)

    def test_rejects_empty_artifact_types(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")
        with pytest.raises(PluginRegistrationError, match="artifact_types"):

            @p.compensate(artifact_types=[])
            async def revert(context, handle):
                return {}

    def test_rejects_overlapping_artifact_types(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")

        @p.compensate(artifact_types=["pr"])
        async def revert_pr(context, handle):
            return {}

        with pytest.raises(PluginRegistrationError, match="overlap"):

            @p.compensate(artifact_types=["pr"])
            async def revert_pr_again(context, handle):
                return {}


class TestDispatchCompensate:
    @pytest.fixture
    def compensating_plugin(self):
        p = plugin(name="github", credentials=[], data_jurisdiction="us")

        @p.compensate(artifact_types=["code", "pr"])
        async def revert_pr(context, handle):
            return {"status": "partially_compensated", "closed": handle.get("number")}

        return p

    async def test_dispatch_routes_by_artifact_type(self, compensating_plugin):
        runner = PluginRunner()
        result = await runner.dispatch_compensate(
            compensating_plugin.meta,
            artifact_type="pr",
            context=_Ctx(),
            handle={"number": 7},
        )
        assert result == {"status": "partially_compensated", "closed": 7}

    async def test_dispatch_raises_when_no_handler(self, compensating_plugin):
        runner = PluginRunner()
        with pytest.raises(PluginRunError, match="compensate"):
            await runner.dispatch_compensate(
                compensating_plugin.meta,
                artifact_type="issue_comment",
                context=_Ctx(),
                handle={},
            )
