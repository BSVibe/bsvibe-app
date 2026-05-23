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
    build_email_event,
    build_notion_event,
    build_slack_event,
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


class TestSlackEventBuilder:
    def test_channel_from_config_text_from_summary(self) -> None:
        shaped = build_slack_event(
            {"summary": "Ship note\nbody line two", "artifact_refs": []},
            {"channel": "C123"},
        )
        assert shaped.artifact_type == "slack_message"
        assert shaped.credential_key == "bot_token"
        # Routing comes from config; text carries the whole summary.
        assert shaped.event["channel"] == "C123"
        assert "Ship note" in shaped.event["text"]
        assert "body line two" in shaped.event["text"]

    def test_artifact_refs_appended_to_text(self) -> None:
        shaped = build_slack_event(
            {"summary": "Spec", "artifact_refs": ["a.md", "b.md"]},
            {"channel": "C1"},
        )
        assert "Artifacts:" in shaped.event["text"]
        assert "- a.md" in shaped.event["text"]
        assert "- b.md" in shaped.event["text"]

    def test_routing_comes_from_config_not_content(self) -> None:
        # Even if content carried a channel (it must not), config is the source.
        shaped = build_slack_event(
            {"summary": "S", "channel": "FROM_CONTENT"},
            {"channel": "FROM_CONFIG"},
        )
        assert shaped.event["channel"] == "FROM_CONFIG"

    def test_missing_channel_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'channel'"):
            build_slack_event({"summary": "S"}, {})


class TestEmailEventBuilder:
    def test_to_from_config_subject_is_first_line_body_is_summary(self) -> None:
        shaped = build_email_event(
            {"summary": "Weekly Update\nthe body line two", "artifact_refs": []},
            {"to": "ceo@bsvibe.dev"},
        )
        assert shaped.artifact_type == "email"
        assert shaped.credential_key == "api_key"
        assert shaped.event["to"] == "ceo@bsvibe.dev"
        assert shaped.event["subject"] == "Weekly Update"
        assert "the body line two" in shaped.event["body"]
        # Plain-text body (no HTML rendering of the summary).
        assert shaped.event["as_text"] is True

    def test_optional_from_passed_through_when_set(self) -> None:
        shaped = build_email_event(
            {"summary": "S"},
            {"to": "x@y.dev", "from": "BSVibe <noreply@bsvibe.dev>"},
        )
        assert shaped.event["from"] == "BSVibe <noreply@bsvibe.dev>"

    def test_from_omitted_when_unset(self) -> None:
        shaped = build_email_event({"summary": "S"}, {"to": "x@y.dev"})
        assert "from" not in shaped.event

    def test_artifact_refs_appended_to_body(self) -> None:
        shaped = build_email_event(
            {"summary": "Spec", "artifact_refs": ["a.md"]},
            {"to": "x@y.dev"},
        )
        assert "Artifacts:" in shaped.event["body"]
        assert "- a.md" in shaped.event["body"]

    def test_routing_comes_from_config_not_content(self) -> None:
        shaped = build_email_event(
            {"summary": "S", "to": "FROM_CONTENT"},
            {"to": "FROM_CONFIG"},
        )
        assert shaped.event["to"] == "FROM_CONFIG"

    def test_missing_to_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'to'"):
            build_email_event({"summary": "S"}, {})


class TestSplitSummary:
    def test_skips_leading_blank_lines(self) -> None:
        title, body = _split_summary("\n\n  Real Title  \nrest")
        assert title == "Real Title"
        assert body == "Real Title  \nrest".strip()


class TestSeam:
    def test_v1_registered_builders(self) -> None:
        # v1 ships notion + slack + email-sender. Other connectors have no entry
        # and are skipped at resolution time. The email connector's key is the
        # plugin name ``email-sender`` (not ``email``) so binding lines up.
        assert set(OUTBOUND_EVENT_BUILDERS) == {"notion", "slack", "email-sender"}


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

    async def test_resolves_slack_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="slack", delivery_config={"channel": "C1"})
            bindings = await _resolve_bindings(
                s, workspace_id=ws, plugins_by_name={"slack": _meta("slack", with_outbound=True)}
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "slack"
        assert bindings[0].builder is build_slack_event

    async def test_resolves_email_sender_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(
                s, workspace_id=ws, connector="email-sender", delivery_config={"to": "a@b.dev"}
            )
            bindings = await _resolve_bindings(
                s,
                workspace_id=ws,
                plugins_by_name={"email-sender": _meta("email-sender", with_outbound=True)},
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "email-sender"
        assert bindings[0].builder is build_email_event


class TestNoLlmGuard:
    async def test_outbound_must_not_call_llm(self) -> None:
        with pytest.raises(RuntimeError, match="must not call the LLM"):
            await _NoLlm().chat("sys", [])
