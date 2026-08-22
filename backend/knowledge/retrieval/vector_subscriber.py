"""노트 임베딩 계산 + 저장.

한때 ``VectorSubscriber`` 클래스가 vault write 이벤트를 구독하는 모양이었지만,
**어디서도 인스턴스화되지 않았다** (backend·tests 통틀어 0). 2026-08-21 에 지웠다.
남은 :func:`embed_and_store_note` 는 :mod:`backend.knowledge.retrieval.reconcile`
이 직접 부른다 — 구독이 아니라 명시적 호출이 실제 배선이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from backend.knowledge.graph.markdown_utils import body_after_frontmatter, extract_frontmatter

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.retrieval.embedder import Embedder
    from backend.knowledge.retrieval.storage.backend import NoteVectorBackend

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_EMBED_CHARS = 8000


async def embed_and_store_note(
    vault: Vault,
    embedder: Embedder,
    vector_store: NoteVectorBackend,
    note_path: str,
    *,
    max_embed_chars: int = _DEFAULT_MAX_EMBED_CHARS,
) -> bool:
    """Read ``note_path`` from the vault, embed its title+body, and store the
    vector. Returns True iff a vector was stored. Soft on every failure (missing
    file, empty text, embed error, empty vector) → False, never raises — shared
    by the event subscriber (live writes) and the reconcile backfill."""
    try:
        abs_path = vault.resolve_path(note_path)
        content = await vault.read_note_content(abs_path)
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        logger.debug("vector_read_failed", path=note_path)
        return False

    fm = extract_frontmatter(content)
    title = fm.get("title", "")
    body = body_after_frontmatter(content)

    text = f"{title}\n{body}".strip()
    if not text:
        return False

    if len(text) > max_embed_chars:
        logger.warning(
            "vector_text_truncated",
            path=note_path,
            original_len=len(text),
            max_len=max_embed_chars,
        )
        text = text[:max_embed_chars]

    try:
        embedding = await embedder.embed(text)
    except (RuntimeError, OSError, ValueError):
        logger.warning("vector_embed_failed", path=note_path, exc_info=True)
        return False
    if not embedding:
        return False
    await vector_store.store(note_path, embedding)
    logger.debug("vector_stored", path=note_path, dim=len(embedding))
    return True
