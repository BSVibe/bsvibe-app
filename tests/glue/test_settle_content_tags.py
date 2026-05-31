"""Tests for content-tag derivation in the settle sink.

``KnowledgeSettleSink`` writes each garden observation with structural tags
(``settle`` / ``verified-run``) plus CONTENT tags that the
``GardenObservationPromoter`` clusters into canonical concepts. Content tags now
come from two paths:

* PRIMARY — LLM-extracted ENTITIES (BSage's mechanism): the sink runs an
  ``EntityExtractor`` over the verified work's summary + intent and uses the
  entity names the LLM committed as ``[[wikilinks]]`` (anti-hallucination
  gated). Generic nouns ("returns", "string", "work") are excluded
  *structurally*, never via a denylist. (``Test*`` classes at the bottom; the
  LLM is MOCKED per the testing rules.)
* SOFT-FALLBACK — the deterministic ``derive_content_tags`` heuristic
  (product slug + intent/summary salient terms + artifact-ref stems), used only
  when no LLM / no active model account is available, or extraction errors /
  yields nothing. The settlement is the source of truth — derivation never
  breaks the write. The retired ``filler_words`` deny-list is NOT applied here.

The fallback's derived tags must pass the canonicalization concept-id grammar
(Handoff §2) so the promoter picks them up — asserted via the real
``TagResolver.normalize`` + ``is_valid_concept_id``.
"""

from __future__ import annotations

import uuid

import pytest

from backend.knowledge.canonicalization.paths import is_valid_concept_id
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.infrastructure.workers.settle_worker import (
    _MAX_CONTENT_TAGS,
    Settlement,
    derive_content_tags,
)

_STRUCTURAL = {"settle", "verified-run"}


def _settlement(
    *,
    summary: str = "",
    artifact_refs: list[str] | None = None,
    product_slug: str | None = None,
    product_name: str | None = None,
    intent_text: str | None = None,
) -> Settlement:
    return Settlement(
        workspace_id=uuid.uuid4(),
        region="us-1",
        run_id=uuid.uuid4(),
        activity_id=uuid.uuid4(),
        verified=True,
        summary=summary,
        artifact_refs=artifact_refs or [],
        product_slug=product_slug,
        product_name=product_name,
        intent_text=intent_text,
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


# --------------------------------------------------------------------------
# Product + intent enrichment (the gap this PR closes): product slug is the
# strongest stable cluster key; intent_text is the founder's own words. Both
# are deterministic stable inputs — never LLM output.
# --------------------------------------------------------------------------


def test_product_slug_is_first_content_tag() -> None:
    """The product slug is the strongest stable cluster key, so it leads the
    derived tags — runs for the SAME product cluster on it regardless of which
    files happened to change."""
    tags = derive_content_tags(
        _settlement(
            product_slug="vaultwarden-selfhost",
            summary="hardened the proxy",
            artifact_refs=["deploy/Caddyfile"],
        )
    )
    assert tags[0] == "vaultwarden-selfhost"
    # The structural-only fallback is gone — artifact/summary tags follow.
    assert "proxy" in tags
    assert "caddyfile" in tags


def test_intent_text_yields_salient_terms() -> None:
    """The founder's intent_text contributes salient terms (same heuristic as
    the summary), so runs sharing intent cluster on what the work was ABOUT."""
    tags = derive_content_tags(
        _settlement(intent_text="Set up the vaultwarden password manager on the mini")
    )
    assert "vaultwarden" in tags
    assert "password" in tags
    assert "manager" in tags
    # Stopwords / short tokens dropped, same discipline as the summary path.
    assert "the" not in tags
    assert "on" not in tags


def test_product_and_intent_both_appear_normalized() -> None:
    tags = derive_content_tags(
        _settlement(
            product_slug="BSVibe-App",
            intent_text="Wire the Settle pipeline!",
            summary="configured tags",
            artifact_refs=["backend/workers/settle_worker.py"],
        )
    )
    # Product slug normalized to a valid concept-id candidate, leads.
    assert tags[0] == "bsvibe-app"
    # Intent salient term present + normalized.
    assert "pipeline" in tags
    assert all(t == t.casefold() for t in tags)
    assert all(not t.startswith("-") and not t.endswith("-") for t in tags)


def test_product_name_used_when_slug_absent() -> None:
    """If the run carries a product name but no slug, the name normalizes into a
    cluster key (defensive: products always have a slug, but be graceful)."""
    tags = derive_content_tags(
        _settlement(product_name="Vault Warden", summary="hardened the proxy")
    )
    assert "vault-warden" in tags


def test_product_slug_preferred_over_name() -> None:
    """Slug is the canonical stable binding; when both are present the slug wins
    as the lead key and the name is not separately emitted (avoids dup signal)."""
    tags = derive_content_tags(
        _settlement(product_slug="vw-host", product_name="Vault Warden", summary="proxy")
    )
    assert tags[0] == "vw-host"
    assert "vault-warden" not in tags


def test_graceful_degradation_no_product_no_intent() -> None:
    """A connector-inbound run (no product, no intent_text) degrades to the
    exact PR #27 behavior: summary + artifact_refs only, no enrichment noise."""
    tags = derive_content_tags(
        _settlement(summary="configured the reverse proxy", artifact_refs=["deploy/Caddyfile"])
    )
    assert "proxy" in tags
    assert "caddyfile" in tags
    # Nothing leaked in from absent product/intent.
    assert "" not in tags


def test_product_intent_deduped_against_summary_and_refs() -> None:
    """If the product slug or an intent term also appears in the summary/refs, it
    is emitted once (first-wins) — product/intent lead, so they win the slot."""
    tags = derive_content_tags(
        _settlement(
            product_slug="auth",
            intent_text="harden auth client refresh",
            summary="auth client work",
            artifact_refs=["backend/auth/client.py"],
        )
    )
    assert tags.count("auth") == 1
    assert tags[0] == "auth"


def test_product_intent_respect_the_cap() -> None:
    long_intent = " ".join(f"concept{i:02d}" for i in range(40))
    tags = derive_content_tags(
        _settlement(product_slug="prod", intent_text=long_intent, summary="more terms here")
    )
    assert len(tags) <= _MAX_CONTENT_TAGS
    # Even when capped, the product slug — the strongest key — is never evicted.
    assert "prod" in tags


def test_enriched_tags_are_valid_concept_id_candidates() -> None:
    tags = derive_content_tags(
        _settlement(
            product_slug="vaultwarden-selfhost",
            intent_text="Set up the vaultwarden password manager",
            summary="hardened the proxy",
            artifact_refs=["deploy/Caddyfile"],
        )
    )
    assert tags
    for tag in tags:
        assert tag not in _STRUCTURAL
        assert TagResolver.normalize(tag) == tag, f"{tag!r} is not a normalize fixed point"
        assert is_valid_concept_id(tag), f"{tag!r} is not a valid concept id"


def test_fallback_keeps_subject_nouns() -> None:
    """The deterministic fallback (``derive_content_tags``) keeps salient subject
    nouns from the summary. It is the SOFT-FALLBACK path used only when no LLM is
    available; the PRIMARY path (LLM entity extraction at the sink) is where
    generic nouns are excluded *structurally*. The fallback no longer carries an
    open-ended filler deny-list (``filler_words`` was retired) — it only drops a
    small set of function-word stopwords, by design (better to keep a borderline
    subject than to over-prune with an unbounded noun list)."""
    tags = derive_content_tags(
        _settlement(summary="I've created a one-line hello world function in Python")
    )
    # Subject nouns survive.
    assert "python" in tags
    assert "hello" in tags
    assert "world" in tags
    assert "function" in tags
    # Function-word stopwords are still dropped (short, high-frequency).
    assert "the" not in tags


def test_fallback_no_longer_applies_a_filler_denylist() -> None:
    """The retired ``filler_words`` deny-list is gone: the deterministic fallback
    is deliberately lenient and does NOT enumerate non-concept words. Words like
    'created' / 'else' that the old denylist dropped now survive the fallback —
    the noise problem is solved at the PRIMARY path (entity extraction), not by a
    denylist on the degraded fallback."""
    tags = derive_content_tags(_settlement(summary="else created the calculator application"))
    # No denylist filtering — formerly-'filler' tokens are kept by the fallback.
    assert "created" in tags
    assert "else" in tags
    # Real subject nouns survive too.
    assert "calculator" in tags
    assert "application" in tags
    # filler_words module must be gone — importing it is an ImportError.
    with pytest.raises(ModuleNotFoundError):
        import backend.knowledge.canonicalization.filler_words  # noqa: F401


def test_structural_product_slug_is_rejected() -> None:
    """A product literally slugged 'settle' must not re-introduce a structural
    marker as a content tag."""
    tags = derive_content_tags(_settlement(product_slug="settle", summary="real work"))
    assert "settle" not in tags


# --------------------------------------------------------------------------
# PRIMARY path: concepts from LLM-extracted ENTITIES (not summary tokens).
# The sink runs an EntityExtractor over summary+intent; the LLM-committed
# entity names (anti-hallucination-gated) become the content tags. Generic
# nouns are excluded structurally. On no-extractor / no-LLM / error / empty,
# the sink SOFT-FALLS BACK to derive_content_tags — settlement never breaks.
# The LLM is MOCKED (per the testing rules — never call a real model).
# --------------------------------------------------------------------------


class _StubExtractor:
    """Mocked EntityExtractor — returns scripted entity names, never an LLM."""

    def __init__(self, names: list[str] | None = None, *, raises: bool = False) -> None:
        self._names = names or []
        self._raises = raises
        self.seen_text: str | None = None

    async def extract_entity_names(self, text: str, *, label: str = "seed") -> list[str]:
        self.seen_text = text
        if self._raises:
            raise RuntimeError("llm exploded")
        return list(self._names)


async def _sink_tags(sink, settlement) -> list[str]:
    """Drive the sink's tag derivation in isolation."""
    return await sink._derive_tags(settlement)


@pytest.mark.asyncio
async def test_sink_uses_extracted_entities_as_content_tags(tmp_path) -> None:
    """With an extractor wired, content tags are the LLM-extracted entity names
    (normalized), NOT summary word-tokens."""
    from backend.knowledge.infrastructure.workers.settle_worker import KnowledgeSettleSink

    extractor = _StubExtractor(["calculator", "Python"])

    async def _factory(
        *,
        region: str,
        workspace_id,
    ):  # noqa: ANN001, ARG001
        return extractor

    sink = KnowledgeSettleSink(vault_root=tmp_path, extractor_factory=_factory)
    settlement = _settlement(
        summary="returns a string from the function",
        intent_text="build a calculator",
    )
    tags = await _sink_tags(sink, settlement)

    # Entity names become the tags (normalized).
    assert tags == ["calculator", "python"]
    # Generic summary nouns ('returns', 'string', 'function') do NOT leak in —
    # they were never extracted as entities.
    assert "returns" not in tags
    assert "string" not in tags
    assert "function" not in tags
    # The extractor saw the intent + summary as seed text.
    assert "calculator" in (extractor.seen_text or "")
    assert "returns a string" in (extractor.seen_text or "")


@pytest.mark.asyncio
async def test_sink_soft_fallback_when_no_extractor(tmp_path) -> None:
    """No extractor factory → deterministic fallback (current behavior)."""
    from backend.knowledge.infrastructure.workers.settle_worker import KnowledgeSettleSink

    sink = KnowledgeSettleSink(vault_root=tmp_path)
    tags = await _sink_tags(sink, _settlement(summary="configured the reverse proxy"))
    assert {"configured", "reverse", "proxy"} <= set(tags)


@pytest.mark.asyncio
async def test_sink_soft_fallback_when_factory_returns_none(tmp_path) -> None:
    """Factory returns None (no active model account) → deterministic fallback."""
    from backend.knowledge.infrastructure.workers.settle_worker import KnowledgeSettleSink

    async def _factory(*, region: str, workspace_id):  # noqa: ANN001, ARG001
        return None

    sink = KnowledgeSettleSink(vault_root=tmp_path, extractor_factory=_factory)
    tags = await _sink_tags(sink, _settlement(summary="configured the reverse proxy"))
    assert {"configured", "reverse", "proxy"} <= set(tags)


@pytest.mark.asyncio
async def test_sink_soft_fallback_when_extractor_raises(tmp_path) -> None:
    """An extractor that raises must NOT break the settle write — the sink logs
    + degrades to the deterministic fallback."""
    from backend.knowledge.infrastructure.workers.settle_worker import KnowledgeSettleSink

    async def _factory(*, region: str, workspace_id):  # noqa: ANN001, ARG001
        return _StubExtractor(raises=True)

    sink = KnowledgeSettleSink(vault_root=tmp_path, extractor_factory=_factory)
    tags = await _sink_tags(sink, _settlement(summary="configured the reverse proxy"))
    # Fell back to the deterministic heuristic instead of raising.
    assert {"configured", "reverse", "proxy"} <= set(tags)


@pytest.mark.asyncio
async def test_sink_soft_fallback_when_no_entities_extracted(tmp_path) -> None:
    """A thin summary yielding zero entities → fallback (never an empty tag set
    when deterministic signal exists)."""
    from backend.knowledge.infrastructure.workers.settle_worker import KnowledgeSettleSink

    async def _factory(*, region: str, workspace_id):  # noqa: ANN001, ARG001
        return _StubExtractor([])

    sink = KnowledgeSettleSink(vault_root=tmp_path, extractor_factory=_factory)
    tags = await _sink_tags(sink, _settlement(summary="configured the reverse proxy"))
    assert {"configured", "reverse", "proxy"} <= set(tags)


@pytest.mark.asyncio
async def test_sink_entity_tags_are_capped_and_normalized(tmp_path) -> None:
    """Extracted entity names are normalized + capped at _MAX_CONTENT_TAGS."""
    from backend.knowledge.infrastructure.workers.settle_worker import (
        _MAX_CONTENT_TAGS,
        KnowledgeSettleSink,
    )

    many = [f"Entity{i:02d}" for i in range(_MAX_CONTENT_TAGS + 5)]

    async def _factory(*, region: str, workspace_id):  # noqa: ANN001, ARG001
        return _StubExtractor(many)

    sink = KnowledgeSettleSink(vault_root=tmp_path, extractor_factory=_factory)
    tags = await _sink_tags(sink, _settlement(summary="x"))
    assert len(tags) <= _MAX_CONTENT_TAGS
    assert all(t == t.casefold() for t in tags)
