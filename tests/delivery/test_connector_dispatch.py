"""Unit tests for the connector-bound outbound delivery seam.

Covers the event-shaping (notion mapper + first-line-title + artifact_refs
append), the no-builder seam (a connector with no v1 mapper is skipped), the
inactive / empty-config resolution rules, and the no-LLM guard. The full
worker → notion HTTP loop is in ``tests/glue/test_connector_deliver_e2e.py``.
"""

from __future__ import annotations

import uuid

import pytest

from backend.connectors.db import ConnectorAccountRow
from backend.delivery.connector_dispatch import (
    OUTBOUND_EVENT_BUILDERS,
    _NoLlm,
    _resolve_bindings,
    _split_summary,
    build_notion_event,
)
from backend.plugins.base import OutboundCapability, PluginMeta

from .._support import memory_session


def _meta(name: str, *, with_outbound: bool) -> PluginMeta:
    outbounds = (
        [OutboundCapability(fn=lambda *_a, **_k: None, artifact_types=("page",))]
        if with_outbound
        else []
    )
    return PluginMeta(
        name=name,
        version="0",
        description="",
        author="t",
        data_jurisdiction="us",
        credentials=[],
        outbounds=outbounds,
    )


async def _seed(session, **kw) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=kw["workspace_id"],
            connector=kw["connector"],
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext="x",
            delivery_config=kw.get("delivery_config", {}),
            is_active=kw.get("is_active", True),
        )
    )
    await session.commit()


class TestNotionEventBuilder:
    def test_title_is_first_line_body_is_full_summary(self) -> None:
        shaped = build_notion_event(
            {"summary": "Quarterly Spec\nbody line two", "artifact_refs": []},
            {"parent_page_id": "P"},
        )
        assert shaped.artifact_type == "page"
        assert shaped.event["parent_page_id"] == "P"
        assert shaped.event["title"] == "Quarterly Spec"
        assert "body line two" in shaped.event["body"]

    def test_artifact_refs_appended_to_body(self) -> None:
        shaped = build_notion_event(
            {"summary": "Spec", "artifact_refs": ["a.md", "b.md"]},
            {"parent_page_id": "P"},
        )
        assert "Artifacts:" in shaped.event["body"]
        assert "- a.md" in shaped.event["body"]
        assert "- b.md" in shaped.event["body"]

    def test_empty_summary_gets_placeholder_title(self) -> None:
        shaped = build_notion_event({"summary": "", "artifact_refs": []}, {"parent_page_id": "P"})
        assert shaped.event["title"] == "Delivered artifact"

    def test_routing_comes_from_config_not_content(self) -> None:
        # Even if content carried a parent_page_id (it must not), config wins.
        shaped = build_notion_event(
            {"summary": "S", "parent_page_id": "FROM_CONTENT"},
            {"parent_page_id": "FROM_CONFIG"},
        )
        assert shaped.event["parent_page_id"] == "FROM_CONFIG"


class TestSplitSummary:
    def test_skips_leading_blank_lines(self) -> None:
        title, body = _split_summary("\n\n  Real Title  \nrest")
        assert title == "Real Title"
        assert body == "Real Title  \nrest".strip()


class TestSeam:
    def test_only_notion_registered_in_v1(self) -> None:
        # The deliberate seam: only notion ships a v1 builder. Other connectors
        # have no entry and are skipped at resolution time.
        assert set(OUTBOUND_EVENT_BUILDERS) == {"notion"}


class TestResolution:
    async def test_skips_empty_delivery_config(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="notion", delivery_config={})
            bindings = await _resolve_bindings(
                s, workspace_id=ws, plugins_by_name={"notion": _meta("notion", with_outbound=True)}
            )
        assert bindings == []

    async def test_skips_connector_without_outbound_plugin(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="slack", delivery_config={"x": 1})
            bindings = await _resolve_bindings(
                s, workspace_id=ws, plugins_by_name={"slack": _meta("slack", with_outbound=False)}
            )
        assert bindings == []

    async def test_skips_connector_with_no_v1_builder(self) -> None:
        # An outbound-capable connector with no registered event-builder is the
        # deliberate seam — skipped (logged), not delivered.
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="github", delivery_config={"x": 1})
            bindings = await _resolve_bindings(
                s, workspace_id=ws, plugins_by_name={"github": _meta("github", with_outbound=True)}
            )
        assert bindings == []

    async def test_resolves_notion_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(
                s, workspace_id=ws, connector="notion", delivery_config={"parent_page_id": "P"}
            )
            bindings = await _resolve_bindings(
                s, workspace_id=ws, plugins_by_name={"notion": _meta("notion", with_outbound=True)}
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "notion"


class TestNoLlmGuard:
    async def test_outbound_must_not_call_llm(self) -> None:
        with pytest.raises(RuntimeError, match="must not call the LLM"):
            await _NoLlm().chat("sys", [])
