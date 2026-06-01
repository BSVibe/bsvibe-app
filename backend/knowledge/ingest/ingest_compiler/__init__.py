"""IngestCompiler — compile knowledge at ingestion time, not query time.

Inspired by Karpathy Wiki: when new data arrives, immediately find and
update/create related garden notes instead of waiting for scheduled skills.

Lift L3 (v8 §17.6) split the previously 894-LOC ``ingest_compiler.py``
into a package whose internals live in sibling modules:

- :mod:`._chunking` — partition + budget probe (``BatchItem``,
  ``_chunk_batch``, ``derive_batch_char_budget``).
- :mod:`._related_context` — per-chunk vault retrieval. ⚠️  Called once
  PER chunk inside the chunk loop. See ``rag-batch-stale-related-context``.
- :mod:`._llm_compile` — LLM seam (``CompileLlm``), system prompt,
  per-chunk user-message assembly, JSON response parsing, and
  post-parse hygiene (``clean_tags`` / ``clean_entities``).
- :mod:`._actions` — plan execution + supporting data classes
  (``UpdateAction`` / ``CompileResult`` / ``IngestBatchRecord``).
- :mod:`._compiler` — :class:`IngestCompiler` orchestration.

This module re-exports the public surface so every external import path
that used the old single-module shape continues to resolve unchanged.
Private helpers are re-exported under their original underscore names
so tests that ``monkeypatch.setattr(ingest_compiler, "_foo", ...)`` keep
intercepting calls — see ``test_uses_litellm_registry_for_known_model``.
"""

from __future__ import annotations

from ._actions import (  # noqa: F401  -- re-exported for facade-shape compat
    _REQUIRED_ACTION_FIELDS,
    CompileResult,
    IngestBatchRecord,
    IngestBatchRecorder,
    UpdateAction,
)
from ._actions import (  # noqa: F401
    empty_compile_result as _empty_compile_result,
)
from ._chunking import (  # noqa: F401  -- re-exported for facade-shape + monkeypatch
    _BUDGET_SAFETY_FRACTION,
    _CHARS_PER_TOKEN,
    _DEFAULT_BATCH_CHAR_BUDGET,
    _OLLAMA_BUDGET_CAP,
    BatchItem,
    _chunk_batch,
    _litellm_max_input_tokens,
    _ollama_context_length,
    _probe_max_input_tokens,
    _truncate_item,
    derive_batch_char_budget,
)
from ._compiler import (  # noqa: F401  -- re-exported for facade-shape compat
    CompileLlm,
    IngestCompiler,
    LLMClient,
)
from ._llm_compile import (  # noqa: F401  -- re-exported for facade-shape compat
    _KIND_TAG_BLOCKLIST,
    _MAX_TAGS_PER_ACTION,
    _TAG_PATTERN,
    _WIKILINK_PATTERN,
    COMPILE_BATCH_SYSTEM_PROMPT,
)
from ._llm_compile import (  # noqa: F401
    clean_entities as _clean_entities,
)
from ._llm_compile import (  # noqa: F401
    clean_tags as _clean_tags,
)
from ._llm_compile import (  # noqa: F401
    parse_plan as _parse_plan,
)
from ._related_context import find_related as _find_related  # noqa: F401

__all__ = [
    "COMPILE_BATCH_SYSTEM_PROMPT",
    "BatchItem",
    "CompileLlm",
    "CompileResult",
    "IngestBatchRecord",
    "IngestBatchRecorder",
    "IngestCompiler",
    "LLMClient",
    "UpdateAction",
    "derive_batch_char_budget",
]
