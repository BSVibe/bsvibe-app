"""Batch chunking + char-budget probing for :mod:`ingest_compiler`.

Lift L3 (v8 §17.6) extracts the chunk-sizing concern out of the 894-LOC
monolith. The compile path's chunk loop calls :func:`_chunk_batch` once
per ``compile_batch`` invocation — every chunk it returns then gets its
own related-context lookup + LLM call inside the loop in
:class:`~backend.knowledge.ingest.ingest_compiler._compiler.IngestCompiler`.

Critical invariant (``rag-batch-stale-related-context``): this module
makes NO calls to the retriever and produces NO LLM input — it only
*partitions* items. Related context belongs to the per-chunk loop, not
here.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


# Conservative fallback when nothing better is known about the model
# (no probe, no override). Tuned for small local LLMs — large frontier
# models will set their own budget via :func:`derive_batch_char_budget`
# at AppState construction time. Single items larger than the budget
# are truncated rather than allowed to balloon the prompt.
#
# Lift E14 halved this from 5_000 to 2_500 because executor-adapter
# calls were subprocess-spawn-per-call (~minutes of overhead) and the
# per-caller timeout was 180 s — bigger chunks risked timing out before
# the LLM finished a single big file.
#
# Lift E18 raises it to 16_000 because the executor adapter is now
# ``opencode serve`` + HTTP (Lift E17): per-call overhead dropped from
# minutes to ~1 s, the caller timeout is 600 s, and ``opencode-go``
# subscription has effectively unlimited token budget. Big chunks pay
# off — fewer LLM calls per bootstrap, same wall-clock per call, more
# context for the LLM to deduplicate/cross-reference across seeds.
_DEFAULT_BATCH_CHAR_BUDGET = 16_000


# Reserve this fraction of the model's input window for the system
# prompt, the per-batch frame text, and headroom for the JSON output —
# the rest gets allocated to seed payload.
_BUDGET_SAFETY_FRACTION = 0.4

# Rough chars-per-token for ASCII-heavy markdown. Korean/CJK runs ~2,
# but we're erring conservative on input length, not output.
_CHARS_PER_TOKEN = 3.5

# Local ollama models often DECLARE huge context windows (200k+) but
# actually generate slowly past a few thousand chars of input. The cap
# bounds what we hand a small local model — hosted models
# (anthropic/openai/etc.) are unaffected and pick up the full derived
# budget.
#
# Lift E14 → E18 path: was 8_000, then halved to 4_000 under the slow
# subprocess-spawn executor (Lift E14), now raised to 24_000 because
# Lift E17 swapped to ``opencode serve`` + HTTP (per-call overhead ~1 s,
# 600 s caller timeout). Bigger chunks mean fewer LLM calls per bootstrap
# — the dogfood win is 5-6× fewer chunks on bsvibe-app (1309 → 200-300).
_OLLAMA_BUDGET_CAP = 24_000


@dataclass
class BatchItem:
    """One labelled chunk fed to :meth:`IngestCompiler.compile_batch`.

    ``label`` is a human-readable identifier (e.g. filename) so the LLM
    can reference seeds in its reasoning. ``content`` is the raw seed
    text the LLM should consider.
    """

    label: str
    content: str


def _chunk_batch(items: list[BatchItem], char_budget: int) -> list[list[BatchItem]]:
    """Split items into chunks whose total content stays under char_budget.

    Items larger than the budget are truncated (with a marker) so a
    single mega-file (e.g. an index page) can't blow up the prompt and
    starve the LLM. Order is preserved so seed numbering stays
    meaningful within each chunk.
    """
    chunks: list[list[BatchItem]] = []
    current: list[BatchItem] = []
    current_size = 0
    for raw_item in items:
        item = _truncate_item(raw_item, char_budget)
        item_size = len(item.content)
        if current and current_size + item_size > char_budget:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += item_size
    if current:
        chunks.append(current)
    return chunks


def _truncate_item(item: BatchItem, max_chars: int) -> BatchItem:
    """Cap an oversized item's content so it can fit in a single chunk."""
    if len(item.content) <= max_chars:
        return item
    head = item.content[: max_chars - 80]
    return BatchItem(
        label=item.label,
        content=f"{head}\n\n…[truncated for batch budget; original was {len(item.content)} chars]…",
    )


async def derive_batch_char_budget(
    model: str,
    api_base: str | None = None,
    *,
    fallback: int = _DEFAULT_BATCH_CHAR_BUDGET,
) -> int:
    """Probe the configured model for its context window, return a safe budget.

    Looks up the input-token limit (ollama via ``/api/show``, others via
    litellm's static model registry) and converts to a char budget that
    keeps room for the system prompt + LLM output. Falls back to
    ``fallback`` if probing fails.

    Computed once at AppState construction time and passed into
    :class:`IngestCompiler` — runtime model swaps trigger a re-probe at
    the same boundary.
    """
    max_input_tokens = await _probe_max_input_tokens(model, api_base)
    if max_input_tokens is None:
        logger.info("ingest_batch_budget_fallback", model=model, chars=fallback)
        return fallback
    budget = int(max_input_tokens * _CHARS_PER_TOKEN * _BUDGET_SAFETY_FRACTION)
    # Don't go below the conservative default — micro-models would just
    # produce thrashing chunks otherwise.
    budget = max(budget, _DEFAULT_BATCH_CHAR_BUDGET)
    # Local ollama models advertise huge contexts but generate slowly
    # past a few thousand input chars. Cap them so we don't ship a
    # technically-correct-but-practically-broken single 200k char
    # prompt to a small local model.
    if model.startswith(("ollama/", "ollama_chat/")):
        budget = min(budget, _OLLAMA_BUDGET_CAP)
    logger.info(
        "ingest_batch_budget_derived",
        model=model,
        max_input_tokens=max_input_tokens,
        chars=budget,
    )
    return budget


async def _probe_max_input_tokens(model: str, api_base: str | None) -> int | None:
    """Return max input tokens for the model, or ``None`` if unknown.

    Looks up ``_ollama_context_length`` and ``_litellm_max_input_tokens``
    via the facade module so tests that monkeypatch the package-level
    symbols (``ingest_compiler._litellm_max_input_tokens``) still
    intercept the call after the Lift L3 package decomp.
    """
    # Late import to avoid a package-init cycle; the facade re-exports
    # both helpers, and tests patch them via the facade name. The
    # ``type: ignore`` covers mypy's implicit-re-export check — the
    # facade pins these aliases (with an F-401 unused-import noqa).
    from backend.knowledge.ingest import ingest_compiler as _facade

    if model.startswith(("ollama/", "ollama_chat/")) and api_base:
        return await _facade._ollama_context_length(model, api_base)  # type: ignore[attr-defined]
    return _facade._litellm_max_input_tokens(model)  # type: ignore[attr-defined]


async def _ollama_context_length(model: str, api_base: str) -> int | None:
    """Ask ollama's ``/api/show`` for the model's declared context length."""
    bare_name = model.split("/", 1)[1] if "/" in model else model
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{api_base.rstrip('/')}/api/show", json={"name": bare_name})
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    info = data.get("model_info") or {}
    # ollama returns architecture-keyed entries like "glm.context_length".
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


def _litellm_max_input_tokens(model: str) -> int | None:
    """Look up the model in litellm's static registry."""
    try:
        import litellm

        info = litellm.get_model_info(model)
    except Exception:
        return None
    raw = info.get("max_input_tokens") if isinstance(info, dict) else None
    return raw if isinstance(raw, int) and raw > 0 else None
