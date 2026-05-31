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
    ConnectorDeliveryAdapter,
    GithubBinding,
    _NoLlm,
    _resolve_bindings,
    _split_summary,
    build_discord_event,
    build_email_event,
    build_linear_event,
    build_notion_event,
    build_slack_event,
    build_telegram_event,
    build_trello_event,
    github_remote_url,
    resolve_github_binding,
    run_branch_name,
)
from backend.extensions.plugin.base import OutboundCapability, PluginMeta

from .._support import memory_session


class _FakeCipher:
    """A no-op cipher — the defensive branches return before any decrypt."""

    def decrypt(self, token: str) -> str:
        return "tok"


def _account() -> ConnectorAccountRow:
    return ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connector="github",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="x",
        delivery_config={"repo": "owner/name"},
        is_active=True,
    )


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


class TestTelegramEventBuilder:
    def test_chat_id_from_config_text_from_summary(self) -> None:
        shaped = build_telegram_event(
            {"summary": "Ship note\nbody line two", "artifact_refs": []},
            {"chat_id": "12345"},
        )
        assert shaped.artifact_type == "telegram_message"
        assert shaped.credential_key == "bot_token"
        assert shaped.event["chat_id"] == "12345"
        assert "Ship note" in shaped.event["text"]
        assert "body line two" in shaped.event["text"]

    def test_artifact_refs_appended_to_text(self) -> None:
        shaped = build_telegram_event(
            {"summary": "Spec", "artifact_refs": ["a.md", "b.md"]},
            {"chat_id": "C1"},
        )
        assert "Artifacts:" in shaped.event["text"]
        assert "- a.md" in shaped.event["text"]
        assert "- b.md" in shaped.event["text"]

    def test_routing_comes_from_config_not_content(self) -> None:
        shaped = build_telegram_event(
            {"summary": "S", "chat_id": "FROM_CONTENT"},
            {"chat_id": "FROM_CONFIG"},
        )
        assert shaped.event["chat_id"] == "FROM_CONFIG"

    def test_missing_chat_id_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'chat_id'"):
            build_telegram_event({"summary": "S"}, {})


class TestDiscordEventBuilder:
    def test_channel_id_from_config_content_from_summary(self) -> None:
        shaped = build_discord_event(
            {"summary": "Ship note\nbody line two", "artifact_refs": []},
            {"channel_id": "9988"},
        )
        assert shaped.artifact_type == "discord_message"
        assert shaped.credential_key == "bot_token"
        assert shaped.event["channel_id"] == "9988"
        assert "Ship note" in shaped.event["content"]
        assert "body line two" in shaped.event["content"]

    def test_artifact_refs_appended_to_content(self) -> None:
        shaped = build_discord_event(
            {"summary": "Spec", "artifact_refs": ["a.md"]},
            {"channel_id": "C1"},
        )
        assert "Artifacts:" in shaped.event["content"]
        assert "- a.md" in shaped.event["content"]

    def test_routing_comes_from_config_not_content(self) -> None:
        shaped = build_discord_event(
            {"summary": "S", "channel_id": "FROM_CONTENT"},
            {"channel_id": "FROM_CONFIG"},
        )
        assert shaped.event["channel_id"] == "FROM_CONFIG"

    def test_missing_channel_id_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'channel_id'"):
            build_discord_event({"summary": "S"}, {})


class TestLinearEventBuilder:
    def test_team_id_from_config_title_and_description_from_summary(self) -> None:
        shaped = build_linear_event(
            {"summary": "Fix the bug\nthe description body", "artifact_refs": []},
            {"team_id": "TEAM-1"},
        )
        assert shaped.artifact_type == "issue"
        assert shaped.credential_key == "api_key"
        assert shaped.event["team_id"] == "TEAM-1"
        assert shaped.event["title"] == "Fix the bug"
        assert "the description body" in shaped.event["description"]

    def test_artifact_refs_appended_to_description(self) -> None:
        shaped = build_linear_event(
            {"summary": "Spec", "artifact_refs": ["a.md"]},
            {"team_id": "T1"},
        )
        assert "Artifacts:" in shaped.event["description"]
        assert "- a.md" in shaped.event["description"]

    def test_routing_comes_from_config_not_content(self) -> None:
        shaped = build_linear_event(
            {"summary": "S", "team_id": "FROM_CONTENT"},
            {"team_id": "FROM_CONFIG"},
        )
        assert shaped.event["team_id"] == "FROM_CONFIG"

    def test_missing_team_id_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'team_id'"):
            build_linear_event({"summary": "S"}, {})


class TestTrelloEventBuilder:
    def test_list_id_from_config_title_and_desc_from_summary(self) -> None:
        shaped = build_trello_event(
            {"summary": "Card title\nthe card body", "artifact_refs": []},
            {"list_id": "LIST-1", "api_key": "tk_app_key"},
        )
        assert shaped.artifact_type == "card"
        # The secret half (token) lands in the credential slot; the non-secret
        # api_key is carried in extra_credentials, sourced from delivery_config.
        assert shaped.credential_key == "token"
        assert shaped.extra_credentials == {"api_key": "tk_app_key"}
        assert shaped.event["list_id"] == "LIST-1"
        assert shaped.event["title"] == "Card title"
        assert "the card body" in shaped.event["desc"]

    def test_artifact_refs_appended_to_desc(self) -> None:
        shaped = build_trello_event(
            {"summary": "Spec", "artifact_refs": ["a.md"]},
            {"list_id": "L1", "api_key": "k"},
        )
        assert "Artifacts:" in shaped.event["desc"]
        assert "- a.md" in shaped.event["desc"]

    def test_routing_comes_from_config_not_content(self) -> None:
        shaped = build_trello_event(
            {"summary": "S", "list_id": "FROM_CONTENT"},
            {"list_id": "FROM_CONFIG", "api_key": "k"},
        )
        assert shaped.event["list_id"] == "FROM_CONFIG"

    def test_missing_list_id_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'list_id'"):
            build_trello_event({"summary": "S"}, {"api_key": "k"})

    def test_missing_api_key_raises(self) -> None:
        # Trello needs BOTH the key and token; the non-secret api_key comes from
        # the founder-set delivery_config — a missing one is a misconfigured
        # target (the trello client requires both auth params).
        with pytest.raises(ValueError, match="missing required 'api_key'"):
            build_trello_event({"summary": "S"}, {"list_id": "L1"})


class TestSplitSummary:
    def test_skips_leading_blank_lines(self) -> None:
        title, body = _split_summary("\n\n  Real Title  \nrest")
        assert title == "Real Title"
        assert body == "Real Title  \nrest".strip()


class TestSeam:
    def test_v1_registered_builders(self) -> None:
        # Ships notion + slack + email-sender + telegram + discord + linear +
        # trello. Connectors with no entry (github, sentry) are skipped at
        # resolution time. The email connector's key is the plugin name
        # ``email-sender`` (not ``email``) so binding lines up.
        assert set(OUTBOUND_EVENT_BUILDERS) == {
            "notion",
            "slack",
            "email-sender",
            "telegram",
            "discord",
            "linear",
            "trello",
        }


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

    async def test_resolves_telegram_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="telegram", delivery_config={"chat_id": "1"})
            bindings = await _resolve_bindings(
                s,
                workspace_id=ws,
                plugins_by_name={"telegram": _meta("telegram", with_outbound=True)},
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "telegram"
        assert bindings[0].builder is build_telegram_event

    async def test_resolves_discord_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(
                s, workspace_id=ws, connector="discord", delivery_config={"channel_id": "9"}
            )
            bindings = await _resolve_bindings(
                s,
                workspace_id=ws,
                plugins_by_name={"discord": _meta("discord", with_outbound=True)},
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "discord"
        assert bindings[0].builder is build_discord_event

    async def test_resolves_linear_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="linear", delivery_config={"team_id": "T"})
            bindings = await _resolve_bindings(
                s,
                workspace_id=ws,
                plugins_by_name={"linear": _meta("linear", with_outbound=True)},
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "linear"
        assert bindings[0].builder is build_linear_event

    async def test_resolves_trello_binding(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(s, workspace_id=ws, connector="trello", delivery_config={"list_id": "L"})
            bindings = await _resolve_bindings(
                s,
                workspace_id=ws,
                plugins_by_name={"trello": _meta("trello", with_outbound=True)},
            )
        assert len(bindings) == 1
        assert bindings[0].account.connector == "trello"
        assert bindings[0].builder is build_trello_event


class TestNoLlmGuard:
    async def test_outbound_must_not_call_llm(self) -> None:
        with pytest.raises(RuntimeError, match="must not call the LLM"):
            await _NoLlm().chat("sys", [])


class TestGithubHelpers:
    def test_github_remote_url(self) -> None:
        assert github_remote_url("owner/name") == "https://github.com/owner/name.git"

    def test_run_branch_name_is_short_and_stable(self) -> None:
        run_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert run_branch_name(run_id) == "bsvibe/run-12345678"


class TestResolveGithubBinding:
    async def test_resolves_active_github_with_repo(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(
                s,
                workspace_id=ws,
                connector="github",
                delivery_config={"repo": "owner/name", "base_branch": "dev"},
            )
            binding = await resolve_github_binding(s, workspace_id=ws)
        assert binding is not None
        assert binding.repo == "owner/name"
        assert binding.base_branch == "dev"

    async def test_base_branch_defaults_main(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(
                s, workspace_id=ws, connector="github", delivery_config={"repo": "owner/name"}
            )
            binding = await resolve_github_binding(s, workspace_id=ws)
        assert binding is not None and binding.base_branch == "main"

    async def test_no_repo_is_not_a_delivery_target(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            # An inbound-only github binding (no repo) is not a delivery target.
            await _seed(s, workspace_id=ws, connector="github", delivery_config={})
            binding = await resolve_github_binding(s, workspace_id=ws)
        assert binding is None

    async def test_inactive_github_skipped(self) -> None:
        ws = uuid.uuid4()
        async with memory_session() as s:
            await _seed(
                s,
                workspace_id=ws,
                connector="github",
                delivery_config={"repo": "owner/name"},
                is_active=False,
            )
            binding = await resolve_github_binding(s, workspace_id=ws)
        assert binding is None


class TestGithubDeliveryDefensiveBranches:
    """The github delivery handler soft-fails (never wedges) on a misconfigured
    target — mirroring the builder ValueError path the other connectors use."""

    def _adapter(self, **kw: object) -> ConnectorDeliveryAdapter:
        return ConnectorDeliveryAdapter(
            session_factory=None,  # type: ignore[arg-type]  # unused in _deliver_github
            plugins_by_name={},
            cipher=_FakeCipher(),
            **kw,  # type: ignore[arg-type]
        )

    async def test_no_workspace_root_soft_fails(self) -> None:
        adapter = self._adapter()  # workspace_root defaults None
        binding = GithubBinding(account=_account(), repo="owner/name", base_branch="main")
        actions = await adapter._deliver_github(
            binding=binding,
            workspace_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            content={"summary": "S"},
        )
        assert len(actions) == 1 and actions[0].succeeded is False
        assert "workspace_root" in (actions[0].error or "")

    async def test_missing_checkout_soft_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        adapter = self._adapter(workspace_root=tmp_path)
        binding = GithubBinding(account=_account(), repo="owner/name", base_branch="main")
        actions = await adapter._deliver_github(
            binding=binding,
            workspace_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            run_id=uuid.uuid4(),  # no dir created for it
            content={"summary": "S"},
        )
        assert len(actions) == 1 and actions[0].succeeded is False
        assert "checkout does not exist" in (actions[0].error or "")
