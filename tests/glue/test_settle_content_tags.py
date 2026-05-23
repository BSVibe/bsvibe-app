"""Unit tests for the deterministic content-tag derivation in the settle sink.

``KnowledgeSettleSink`` writes each garden observation with the structural tags
``settle`` / ``verified-run`` — which the ``GardenObservationPromoter``
intentionally drops. With only those, the promoter gets zero candidates and the
§5 trust-ratchet loop never produces canon. ``derive_content_tags`` closes that
gap by deriving real content tags from the ``Settlement`` — deterministically,
no LLM/network. These tests pin the heuristic's bounds: artifact-ref stems,
salient summary terms, normalization, dedupe, cap, and the structural-only
fallback for contentless settlements.

The derived tags must also pass the canonicalization concept-id grammar
(Handoff §2) so the promoter actually picks them up — asserted via the real
``TagResolver.normalize`` + ``is_valid_concept_id``.
"""

from __future__ import annotations

import uuid

import pytest

from backend.knowledge.canonicalization.paths import is_valid_concept_id
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.workers.settle_worker import (
    _MAX_CONTENT_TAGS,
    Settlement,
    derive_content_tags,
)

_STRUCTURAL = {"settle", "verified-run"}


def _settlement(*, summary: str = "", artifact_refs: list[str] | None = None) -> Settlement:
    return Settlement(
        workspace_id=uuid.uuid4(),
        region="us-1",
        run_id=uuid.uuid4(),
        activity_id=uuid.uuid4(),
        verified=True,
        summary=summary,
        artifact_refs=artifact_refs or [],
    )


def test_artifact_refs_yield_path_stems() -> None:
    """File paths contribute every component's stem (basename without ext)."""
    tags = derive_content_tags(_settlement(artifact_refs=["backend/auth/client.py"]))
    assert "auth" in tags
    assert "client" in tags
    # The full path is never a tag — only its stems.
    assert "backend/auth/client.py" not in tags


def test_artifact_refs_handle_windows_separators() -> None:
    tags = derive_content_tags(_settlement(artifact_refs=["deploy\\nginx\\site.conf"]))
    assert "nginx" in tags
    assert "site" in tags


def test_summary_yields_salient_terms_dropping_stopwords() -> None:
    """Salient nouns survive; stopwords / short tokens / verbs are dropped."""
    tags = derive_content_tags(_settlement(summary="configured the reverse proxy for vaultwarden"))
    assert "configured" in tags
    assert "reverse" in tags
    assert "proxy" in tags
    assert "vaultwarden" in tags
    # Stopwords + short tokens dropped.
    assert "the" not in tags
    assert "for" not in tags


def test_normalization_lowercases_and_strips_punctuation() -> None:
    tags = derive_content_tags(_settlement(summary="Configured OAuth2! tokens."))
    assert "configured" in tags
    assert "oauth2" in tags
    # No uppercase / trailing punctuation leaks through.
    assert all(t == t.casefold() for t in tags)
    assert all(not t.startswith("-") and not t.endswith("-") for t in tags)


def test_artifact_ref_preserves_hyphenated_stem() -> None:
    """A hyphenated filename stem stays one concept-id token (the canonical
    normalize collapses the separator to a single hyphen)."""
    tags = derive_content_tags(_settlement(artifact_refs=["backend/reverse-proxy.py"]))
    assert "reverse-proxy" in tags


def test_dedupe_preserves_first_appearance_order() -> None:
    tags = derive_content_tags(
        _settlement(
            artifact_refs=["backend/auth/client.py", "backend/auth/server.py"],
            summary="auth auth client refresh",
        )
    )
    # 'auth' appears many times across refs + summary but only once in output,
    # and it comes from the first artifact ref (artifact stems lead summary).
    assert tags.count("auth") == 1
    assert tags.index("auth") < tags.index("refresh")


def test_cap_bounds_the_number_of_content_tags() -> None:
    long_summary = " ".join(f"concept{i:02d}" for i in range(40))
    tags = derive_content_tags(_settlement(summary=long_summary))
    assert len(tags) <= _MAX_CONTENT_TAGS


def test_empty_settlement_yields_no_content_tags() -> None:
    assert derive_content_tags(_settlement()) == []


def test_structural_tags_are_never_derived() -> None:
    """Even if the inputs literally contain the structural markers, they are
    excluded from content tags (the sink adds them separately)."""
    tags = derive_content_tags(
        _settlement(summary="settle verified-run settled", artifact_refs=["settle.py"])
    )
    assert "settle" not in tags
    assert "verified-run" not in tags


def test_leading_digit_tokens_are_dropped() -> None:
    """A normalized tag must start with a letter (Handoff §2 concept-id grammar);
    a pure-numeric / digit-leading token can't anchor a concept, so it's dropped."""
    tags = derive_content_tags(_settlement(summary="bumped 2024 release"))
    assert "2024" not in tags
    assert "release" in tags


@pytest.mark.parametrize(
    ("summary", "refs"),
    [
        ("configured the reverse proxy for vaultwarden", ["backend/auth/client.py"]),
        ("Reverse-Proxy, OAuth2 token refresh", ["deploy/Caddyfile", "src/v2/handler.go"]),
        ("hardened the proxy and self-hosting setup", ["infra/tls/cert.pem"]),
    ],
)
def test_every_derived_tag_is_a_valid_concept_id_candidate(summary, refs) -> None:
    """The promoter only seeds concepts for tags that survive its normalize +
    concept-id validity gate — so every derived tag must already be valid (and
    a fixed point under the resolver's normalization)."""
    tags = derive_content_tags(_settlement(summary=summary, artifact_refs=refs))
    assert tags, "expected at least one content tag for this fixture"
    for tag in tags:
        assert tag not in _STRUCTURAL
        assert TagResolver.normalize(tag) == tag, f"{tag!r} is not a normalize fixed point"
        assert is_valid_concept_id(tag), f"{tag!r} is not a valid concept id"
