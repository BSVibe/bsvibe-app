"""Tests for the discord interaction parser — Ed25519 signature verify,
idempotency from interaction id, PING skip, bot-author skip, and TriggerEvent
mapping (Workflow §3.1 / §6 #4 inbound capability).

A throwaway Ed25519 keypair is generated in-test to produce valid/invalid
signatures — no real Discord keys, no network I/O."""

from __future__ import annotations

import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from backend.intake.schema import TriggerEvent
from backend.plugins.implementations.discord.webhook import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookError,
    WebhookSignatureError,
    parse_interaction,
    verify_signature,
)

WORKSPACE = uuid.uuid4()
TIMESTAMP = "1700000000"


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, public_key_hex) for an in-test throwaway keypair."""
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes_raw().hex()
    return private, public_hex


def _sign(private: Ed25519PrivateKey, timestamp: str, raw_body: bytes) -> str:
    return private.sign(timestamp.encode() + raw_body).hex()


def _headers(*, signature: str, timestamp: str = TIMESTAMP) -> dict:
    return {
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
    }


def _command_interaction(interaction_id: str = "100", interaction_type: int = 2) -> bytes:
    return json.dumps(
        {
            "id": interaction_id,
            "type": interaction_type,
            "channel_id": "555",
            "guild_id": "777",
            "member": {"user": {"id": "5", "bot": False, "username": "ada"}},
            "data": {"name": "ping", "type": 1},
        }
    ).encode()


def _ping() -> bytes:
    return json.dumps({"id": "1", "type": 1}).encode()


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        private, public_hex = _keypair()
        body = _command_interaction()
        sig = _sign(private, TIMESTAMP, body)
        verify_signature(public_hex, body, sig, TIMESTAMP)  # no raise

    def test_rejects_wrong_signature(self):
        _, public_hex = _keypair()
        other_private, _ = _keypair()  # signs with a DIFFERENT key
        body = _command_interaction()
        bad_sig = _sign(other_private, TIMESTAMP, body)
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_signature(public_hex, body, bad_sig, TIMESTAMP)

    def test_rejects_tampered_body(self):
        private, public_hex = _keypair()
        body = _command_interaction()
        sig = _sign(private, TIMESTAMP, body)
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_signature(public_hex, body + b"tampered", sig, TIMESTAMP)

    def test_rejects_missing_signature_header(self):
        _, public_hex = _keypair()
        with pytest.raises(WebhookSignatureError, match="missing X-Signature-Ed25519"):
            verify_signature(public_hex, b"{}", None, TIMESTAMP)

    def test_rejects_missing_timestamp_header(self):
        private, public_hex = _keypair()
        sig = _sign(private, TIMESTAMP, b"{}")
        with pytest.raises(WebhookSignatureError, match="missing X-Signature-Timestamp"):
            verify_signature(public_hex, b"{}", sig, None)

    def test_rejects_non_hex_signature(self):
        _, public_hex = _keypair()
        with pytest.raises(WebhookSignatureError, match="hex"):
            verify_signature(public_hex, b"{}", "not-hex-zz", TIMESTAMP)

    def test_rejects_malformed_public_key(self):
        with pytest.raises(WebhookSignatureError, match="public key"):
            verify_signature("ab" * 8, b"{}", "ab" * 32, TIMESTAMP)  # 8-byte key

    def test_header_names_are_lowercased_canonical(self):
        assert SIGNATURE_HEADER == "x-signature-ed25519"
        assert TIMESTAMP_HEADER == "x-signature-timestamp"


class TestParseInteraction:
    def test_parses_command_into_trigger_event(self):
        private, public_hex = _keypair()
        body = _command_interaction(interaction_id="100")
        sig = _sign(private, TIMESTAMP, body)
        evt = parse_interaction(
            workspace_id=WORKSPACE,
            headers=_headers(signature=sig),
            raw_body=body,
            public_key=public_hex,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.source == "discord"
        assert evt.trigger_kind == "webhook"
        assert evt.idempotency_key == "discord:100"
        assert evt.workspace_id == WORKSPACE
        assert evt.payload["interaction_type"] == 2
        assert evt.payload["channel_id"] == "555"
        assert evt.payload["command_name"] == "ping"

    def test_idempotency_key_from_interaction_id(self):
        private, public_hex = _keypair()
        body = _command_interaction(interaction_id="777")
        sig = _sign(private, TIMESTAMP, body)
        first = parse_interaction(
            workspace_id=WORKSPACE,
            headers=_headers(signature=sig),
            raw_body=body,
            public_key=public_hex,
        )
        second = parse_interaction(
            workspace_id=WORKSPACE,
            headers=_headers(signature=sig),
            raw_body=body,
            public_key=public_hex,
        )
        assert first is not None and second is not None
        assert first.idempotency_key == second.idempotency_key == "discord:777"

    def test_bad_signature_raises(self):
        _, public_hex = _keypair()
        other_private, _ = _keypair()
        body = _command_interaction()
        bad_sig = _sign(other_private, TIMESTAMP, body)
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            parse_interaction(
                workspace_id=WORKSPACE,
                headers=_headers(signature=bad_sig),
                raw_body=body,
                public_key=public_hex,
            )

    def test_ping_returns_none_but_verifies(self):
        # PING must still pass verification, then parse returns None (the HTTP
        # route answers with a PONG — out of this track's scope).
        private, public_hex = _keypair()
        body = _ping()
        sig = _sign(private, TIMESTAMP, body)
        evt = parse_interaction(
            workspace_id=WORKSPACE,
            headers=_headers(signature=sig),
            raw_body=body,
            public_key=public_hex,
        )
        assert evt is None

    def test_ping_with_bad_signature_still_rejected(self):
        # Even the PING handshake must verify — a forged PING is rejected.
        _, public_hex = _keypair()
        other_private, _ = _keypair()
        body = _ping()
        bad_sig = _sign(other_private, TIMESTAMP, body)
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            parse_interaction(
                workspace_id=WORKSPACE,
                headers=_headers(signature=bad_sig),
                raw_body=body,
                public_key=public_hex,
            )

    def test_no_public_key_skips_verification(self):
        body = _command_interaction(interaction_id="200")
        evt = parse_interaction(
            workspace_id=WORKSPACE,
            headers={},  # no signature headers at all
            raw_body=body,
            public_key=None,
        )
        assert evt is not None
        assert evt.idempotency_key == "discord:200"

    def test_unsupported_interaction_type_returns_none(self):
        body = json.dumps({"id": "300", "type": 99}).encode()
        evt = parse_interaction(workspace_id=WORKSPACE, headers={}, raw_body=body, public_key=None)
        assert evt is None

    def test_bot_authored_interaction_skipped(self):
        body = json.dumps(
            {
                "id": "400",
                "type": 2,
                "channel_id": "555",
                "member": {"user": {"id": "9", "bot": True}},
                "data": {"name": "x"},
            }
        ).encode()
        evt = parse_interaction(workspace_id=WORKSPACE, headers={}, raw_body=body, public_key=None)
        assert evt is None

    def test_dm_user_interaction_parses(self):
        # A DM interaction has the user at body["user"], not body["member"].
        body = json.dumps(
            {
                "id": "500",
                "type": 2,
                "channel_id": "dm1",
                "user": {"id": "5", "bot": False},
                "data": {"name": "hi"},
            }
        ).encode()
        evt = parse_interaction(workspace_id=WORKSPACE, headers={}, raw_body=body, public_key=None)
        assert evt is not None
        assert evt.payload["user_id"] == "5"

    def test_missing_interaction_id_raises(self):
        body = json.dumps({"type": 2, "channel_id": "1"}).encode()
        with pytest.raises(WebhookError, match="interaction id"):
            parse_interaction(workspace_id=WORKSPACE, headers={}, raw_body=body, public_key=None)

    def test_malformed_json_raises(self):
        with pytest.raises(WebhookError, match="JSON"):
            parse_interaction(
                workspace_id=WORKSPACE,
                headers={},
                raw_body=b"{not json",
                public_key=None,
            )


def test_public_key_helper_roundtrips():
    # Sanity: the in-test keypair produces a 32-byte raw public key.
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes_raw()
    assert len(raw) == 32
    assert Ed25519PublicKey.from_public_bytes(raw) is not None
