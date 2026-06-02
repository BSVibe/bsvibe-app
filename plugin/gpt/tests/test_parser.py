"""Parser tests for the ChatGPT (GPT) export connector.

Defensive parsing: the parser must always return a list + a skipped
count, never raise. Schema variations covered:

* Missing ``id`` → skipped
* Missing ``mapping`` → skipped
* Root → user → assistant linear path → both messages preserved in order
* ``system`` role message → dropped (meta-instruction)
* ``tool`` role message → kept, sender mapped to ``tool``
* Multimodal content (text + image asset pointer) → text preserved,
  pointer recorded as an attachment
* Branched conversation (two children under one node) → only the
  first-child path is followed
* Structural nodes with ``message=None`` → walked through, no crash
* ``since`` filter accepts BOTH Unix epoch AND ISO string
* Conversations with no real messages → skipped
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.gpt.parser import (
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
                "id": "x",
                "title": "T",
                "create_time": 1712927400.0,
                "update_time": 1712927500.0,
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["m1"],
                    },
                    "m1": {
                        "id": "m1",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["hi"]},
                            "create_time": 1712927400.0,
                        },
                        "parent": "root",
                        "children": ["m2"],
                    },
                    "m2": {
                        "id": "m2",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["hello"]},
                            "create_time": 1712927450.0,
                        },
                        "parent": "m1",
                        "children": [],
                    },
                },
            }
        )
        assert isinstance(convo, ConversationDto)
        assert convo.uuid == "x"
        assert convo.title == "T"
        assert len(convo.messages) == 2
        assert convo.messages[0].sender == "user"
        assert convo.messages[0].text == "hi"
        assert convo.messages[1].sender == "assistant"
        assert convo.messages[1].text == "hello"

    def test_missing_id_returns_none(self):
        convo = parse_conversation({"title": "T", "mapping": {}})
        assert convo is None

    def test_missing_mapping_returns_none(self):
        convo = parse_conversation({"id": "x", "title": "T"})
        assert convo is None

    def test_not_a_dict_returns_none(self):
        assert parse_conversation("not a dict") is None  # type: ignore[arg-type]
        assert parse_conversation(None) is None  # type: ignore[arg-type]

    def test_system_role_message_is_dropped(self):
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["s"],
                    },
                    "s": {
                        "id": "s",
                        "message": {
                            "author": {"role": "system"},
                            "content": {"content_type": "text", "parts": ["meta"]},
                        },
                        "parent": "root",
                        "children": ["u"],
                    },
                    "u": {
                        "id": "u",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["real"]},
                        },
                        "parent": "s",
                        "children": [],
                    },
                },
            }
        )
        assert convo is not None
        assert len(convo.messages) == 1
        assert convo.messages[0].sender == "user"
        assert convo.messages[0].text == "real"

    def test_tool_role_message_kept(self):
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["t"],
                    },
                    "t": {
                        "id": "t",
                        "message": {
                            "author": {"role": "tool"},
                            "content": {
                                "content_type": "text",
                                "parts": ["[search results]"],
                            },
                        },
                        "parent": "root",
                        "children": [],
                    },
                },
            }
        )
        assert convo is not None
        assert convo.messages[0].sender == "tool"
        assert convo.messages[0].text == "[search results]"

    def test_multimodal_text_records_attachments(self):
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["m"],
                    },
                    "m": {
                        "id": "m",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {
                                "content_type": "multimodal_text",
                                "parts": [
                                    "Here you go",
                                    {
                                        "content_type": "image_asset_pointer",
                                        "asset_pointer": "file-service://img-001",
                                    },
                                ],
                            },
                        },
                        "parent": "root",
                        "children": [],
                    },
                },
            }
        )
        assert convo is not None
        msg = convo.messages[0]
        assert msg.text == "Here you go"
        assert msg.attachments == ["file-service://img-001"]

    def test_branched_takes_first_child_path(self):
        # Root has two children — parser must walk only the first.
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["A", "B"],
                    },
                    "A": {
                        "id": "A",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["A-text"]},
                        },
                        "parent": "root",
                        "children": [],
                    },
                    "B": {
                        "id": "B",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["B-text"]},
                        },
                        "parent": "root",
                        "children": [],
                    },
                },
            }
        )
        assert convo is not None
        assert len(convo.messages) == 1
        assert convo.messages[0].text == "A-text"

    def test_unknown_role_becomes_other(self):
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["u"],
                    },
                    "u": {
                        "id": "u",
                        "message": {
                            "author": {"role": "moderator"},
                            "content": {"content_type": "text", "parts": ["x"]},
                        },
                        "parent": "root",
                        "children": [],
                    },
                },
            }
        )
        assert convo is not None
        assert convo.messages[0].sender == "other"

    def test_empty_parts_message_dropped(self):
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["empty"],
                    },
                    "empty": {
                        "id": "empty",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": [""]},
                        },
                        "parent": "root",
                        "children": ["kept"],
                    },
                    "kept": {
                        "id": "kept",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["kept"]},
                        },
                        "parent": "empty",
                        "children": [],
                    },
                },
            }
        )
        assert convo is not None
        assert len(convo.messages) == 1
        assert convo.messages[0].text == "kept"

    def test_title_falls_back_to_untitled(self):
        convo = parse_conversation(
            {
                "id": "x",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["hi"]},
                        },
                        "parent": None,
                        "children": [],
                    }
                },
            }
        )
        assert convo is not None
        assert convo.title == "Untitled"

    def test_epoch_timestamps_converted_to_iso(self):
        convo = parse_conversation(
            {
                "id": "x",
                "title": "T",
                "create_time": 1712927400,  # int
                "update_time": 1712927500.5,  # float
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["hi"]},
                            "create_time": 1712927400.0,
                        },
                        "parent": None,
                        "children": [],
                    }
                },
            }
        )
        assert convo is not None
        # Spec-mandated check #6 — Unix epoch → ISO string.
        assert convo.created_at is not None and "T" in convo.created_at
        assert convo.created_at.endswith("Z")
        assert convo.messages[0].created_at is not None
        assert convo.messages[0].created_at.endswith("Z")


# ── parse_export (full fixture) ───────────────────────────────────────────


class TestParseExport:
    def test_fixture_yields_two_parsed_one_skipped(self, payload):
        convos, skipped = parse_export(payload)
        # Three conversations: conv-abc-123, conv-def-456, missing-id (skipped).
        assert len(convos) == 2
        assert skipped == 1
        uuids = {c.uuid for c in convos}
        assert uuids == {"conv-abc-123", "conv-def-456"}

    def test_fixture_system_message_skipped_in_first_conv(self, payload):
        convos, _ = parse_export(payload)
        first = next(c for c in convos if c.uuid == "conv-abc-123")
        # Fixture has system + user + assistant → only 2 messages survive.
        assert len(first.messages) == 2
        assert first.messages[0].sender == "user"
        assert first.messages[1].sender == "assistant"

    def test_fixture_tool_call_surfaces_in_second_conv(self, payload):
        convos, _ = parse_export(payload)
        second = next(c for c in convos if c.uuid == "conv-def-456")
        senders = [m.sender for m in second.messages]
        assert "tool" in senders
        # Last message is multimodal — attachment recorded.
        assistant = next(m for m in second.messages if m.sender == "assistant")
        assert assistant.attachments == ["file-service://img-001"]

    def test_since_filter_with_epoch_number(self, payload):
        # conv-abc-123 update_time=1713187200, conv-def-456=1713200000.
        convos, skipped = parse_export(payload, since=1713190000)
        assert {c.uuid for c in convos} == {"conv-def-456"}
        assert skipped >= 2  # 1 missing-id + 1 below cutoff

    def test_since_filter_with_iso_string(self, payload):
        # 1713190000 ≈ 2024-04-15T13:26:40Z; pick an ISO past both convs.
        convos, _ = parse_export(payload, since="2030-01-01T00:00:00Z")
        assert convos == []

    def test_non_list_payload_returns_empty(self):
        convos, skipped = parse_export({"not": "a list"})
        assert convos == []
        assert skipped == 0

    def test_zero_message_conversation_is_skipped(self):
        payload = [
            {
                "id": "empty",
                "title": "E",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": [],
                    }
                },
            },
            {
                "id": "kept",
                "title": "K",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["hi"]},
                        },
                        "parent": None,
                        "children": [],
                    }
                },
            },
        ]
        convos, skipped = parse_export(payload)
        assert [c.uuid for c in convos] == ["kept"]
        assert skipped == 1

    def test_cyclic_mapping_does_not_loop_forever(self):
        # Defensive: a (theoretical) cycle must terminate.
        payload = [
            {
                "id": "loopy",
                "title": "L",
                "mapping": {
                    "a": {
                        "id": "a",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["a"]},
                        },
                        "parent": None,
                        "children": ["b"],
                    },
                    "b": {
                        "id": "b",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["b"]},
                        },
                        "parent": "a",
                        "children": ["a"],
                    },
                },
            }
        ]
        convos, _ = parse_export(payload)
        assert len(convos) == 1
        assert [m.text for m in convos[0].messages] == ["a", "b"]

    def test_messagedto_is_dataclass(self):
        m = MessageDto(sender="user", text="x")
        assert m.created_at is None
        assert m.attachments == []
