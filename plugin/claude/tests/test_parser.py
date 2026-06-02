"""Parser tests for the Claude export connector.

Defensive parsing: the parser must always return a list + a skipped
count, never raise. Schema variations covered:

* Missing ``uuid`` → skipped
* Missing ``chat_messages`` → skipped
* String ``text`` payload → preserved verbatim
* Content-block array ``text`` payload → joined into one string
* Unknown ``sender`` value → mapped to ``other``
* ``since`` filter against ``updated_at``
* Empty conversations (zero non-blank messages) → skipped
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.claude.parser import (
    ConversationDto,
    MessageDto,
    parse_conversation,
    parse_export,
)

FIXTURE = Path(__file__).parent / "fixtures" / "conversations_sample.json"


@pytest.fixture
def payload() -> list[dict]:
    return json.loads(FIXTURE.read_text())


# ── parse_conversation ─────────────────────────────────────────────────────


class TestParseConversation:
    def test_parses_canonical_conversation(self):
        convo = parse_conversation(
            {
                "uuid": "x",
                "name": "T",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "chat_messages": [
                    {"text": "hello", "sender": "human"},
                    {"text": "hi", "sender": "assistant"},
                ],
            }
        )
        assert isinstance(convo, ConversationDto)
        assert convo.uuid == "x"
        assert convo.title == "T"
        assert len(convo.messages) == 2
        assert convo.messages[0].sender == "human"
        assert convo.messages[1].sender == "assistant"

    def test_missing_uuid_returns_none(self):
        convo = parse_conversation(
            {"name": "T", "chat_messages": [{"text": "x", "sender": "human"}]}
        )
        assert convo is None

    def test_missing_chat_messages_returns_none(self):
        convo = parse_conversation({"uuid": "x", "name": "T"})
        assert convo is None

    def test_not_a_dict_returns_none(self):
        assert parse_conversation("not a dict") is None  # type: ignore[arg-type]
        assert parse_conversation(None) is None  # type: ignore[arg-type]

    def test_content_block_array_text_is_joined(self):
        convo = parse_conversation(
            {
                "uuid": "x",
                "name": "T",
                "chat_messages": [
                    {
                        "text": [
                            {"type": "text", "text": "first"},
                            {"type": "text", "text": "second"},
                        ],
                        "sender": "assistant",
                    }
                ],
            }
        )
        assert convo is not None
        assert convo.messages[0].text == "first\nsecond"

    def test_unknown_sender_becomes_other(self):
        convo = parse_conversation(
            {
                "uuid": "x",
                "name": "T",
                "chat_messages": [{"text": "tool log", "sender": "tool"}],
            }
        )
        assert convo is not None
        assert convo.messages[0].sender == "other"

    def test_empty_text_messages_are_dropped(self):
        convo = parse_conversation(
            {
                "uuid": "x",
                "name": "T",
                "chat_messages": [
                    {"text": "", "sender": "human"},
                    {"text": None, "sender": "human"},
                    {"text": "kept", "sender": "human"},
                ],
            }
        )
        assert convo is not None
        assert len(convo.messages) == 1
        assert convo.messages[0].text == "kept"

    def test_title_falls_back_to_untitled(self):
        convo = parse_conversation(
            {
                "uuid": "x",
                "chat_messages": [{"text": "x", "sender": "human"}],
            }
        )
        assert convo is not None
        assert convo.title == "Untitled"


# ── parse_export (full fixture) ───────────────────────────────────────────


class TestParseExport:
    def test_fixture_yields_two_parsed_one_skipped(self, payload):
        convos, skipped = parse_export(payload)
        # Three conversations in the fixture: one valid, one missing uuid
        # (skipped), one with mixed senders (valid).
        assert len(convos) == 2
        assert skipped == 1
        uuids = {c.uuid for c in convos}
        assert uuids == {"conv-001", "conv-003"}

    def test_since_filter_skips_old_conversations(self, payload):
        # conv-001 updated 2026-04-20; conv-003 updated 2026-04-15.
        convos, skipped = parse_export(payload, since="2026-04-18")
        assert len(convos) == 1
        assert convos[0].uuid == "conv-001"
        # 1 missing-uuid + 1 below-cutoff = 2 skipped total.
        assert skipped == 2

    def test_since_kwarg_with_z_suffix(self, payload):
        # Equivalent cutoff, but expressed with the trailing Z.
        convos, _ = parse_export(payload, since="2026-04-18T00:00:00Z")
        assert {c.uuid for c in convos} == {"conv-001"}

    def test_non_list_payload_returns_empty(self):
        convos, skipped = parse_export({"not": "a list"})
        assert convos == []
        assert skipped == 0

    def test_zero_message_conversation_is_skipped(self):
        payload = [
            {"uuid": "empty", "name": "E", "chat_messages": []},
            {
                "uuid": "kept",
                "name": "K",
                "chat_messages": [{"text": "hi", "sender": "human"}],
            },
        ]
        convos, skipped = parse_export(payload)
        assert [c.uuid for c in convos] == ["kept"]
        assert skipped == 1

    def test_messagedto_is_dataclass(self):
        m = MessageDto(sender="human", text="x")
        assert m.created_at is None
