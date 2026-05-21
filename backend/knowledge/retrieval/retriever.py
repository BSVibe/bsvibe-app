"""VaultRetriever — index-based 2-step note retrieval with recency fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from backend.knowledge.ingest.index_reader import NoteSummary

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.ingest.index_reader import IndexReader
    from backend.knowledge.retrieval.embedder import Embedder
    from backend.knowledge.retrieval.graph_retriever import GraphRetriever
    from backend.knowledge.retrieval.vector_store import VectorStore

logger = structlog.get_logger(__name__)


class VaultRetriever:
    """Index-based note retrieval.

    Step 1: Read index summaries (title + tags + metadata) for context dirs.
    Step 2: Return summaries as formatted context for LLM to process.

    Falls back to recency-based reading when index is not available.
    """

    def __init__(
        self,
        vault: Vault,
        index_reader: IndexReader | None = None,
        graph_retriever: GraphRetriever | None = None,
        vector_store: VectorStore | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._vault = vault
        self._index_reader = index_reader
        self._graph_retriever = graph_retriever
        self._vector_store = vector_store
        self._embedder = embedder

    @property
    def index_available(self) -> bool:
        """True if an index reader is configured."""
        return self._index_reader is not None

    async def retrieve(
        self,
        query: str,
        context_dirs: list[str],
        max_chars: int = 50_000,
        top_k: int = 20,
    ) -> str:
        """Retrieve vault context as a formatted string.

        When index is available, returns index summaries so the LLM
        can identify relevant notes. Falls back to recency-based reading.

        Args:
            query: The search query (used for logging; LLM does the matching).
            context_dirs: Vault subdirectories to include.
            max_chars: Maximum total characters to return.
            top_k: Maximum number of notes per directory.

        Returns:
            Concatenated note text with ``---`` separators.
        """
        if self._index_reader is not None:
            try:
                return await self._index_retrieve(context_dirs, max_chars, top_k)
            except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError, RuntimeError):
                logger.warning("index_retrieve_failed_fallback", exc_info=True)

        return await self._fallback_retrieve(context_dirs, max_chars, top_k)

    async def search(
        self,
        query: str,
        context_dirs: list[str] | None = None,
        top_k: int = 10,
    ) -> str:
        """Search vault using semantic similarity or index listing.

        When a vector store and embedder are available, ranks results by
        cosine similarity to the query embedding. Otherwise falls back
        to index-based listing for LLM to interpret.

        Args:
            query: Search query.
            context_dirs: Optional filter by directories.
            top_k: Max results.

        Returns:
            Formatted search results string.
        """
        # Try semantic search first
        if self._vector_store is not None and self._embedder is not None and self._embedder.enabled:
            try:
                return await self._vector_search(query, context_dirs, top_k)
            except (RuntimeError, OSError, ValueError):
                logger.warning("vector_search_failed_fallback", exc_info=True)

        if self._index_reader is None:
            dirs = context_dirs or [
                "seeds",
                "garden/seedling",
                "garden/budding",
                "garden/evergreen",
                "garden/entities",
            ]
            return await self._fallback_retrieve(dirs, max_chars=20_000, max_notes_per_dir=top_k)

        if context_dirs:
            summaries: list[NoteSummary] = []
            for d in context_dirs:
                summaries.extend(await self._index_reader.get_summaries(d))
        else:
            summaries = await self._index_reader.get_all_summaries()

        summaries = summaries[:top_k]

        if not summaries:
            return "No notes found."

        lines = [f"Found {len(summaries)} notes (query: {query}):"]
        lines.append("")
        for s in summaries:
            tags_str = ", ".join(f"#{t}" for t in s.tags) if s.tags else ""
            related_str = ", ".join(s.related[:3]) if s.related else ""
            lines.append(f"- **{s.title}** ({s.path})")
            if tags_str:
                lines.append(f"  Tags: {tags_str}")
            if related_str:
                lines.append(f"  Related: {related_str}")
            if s.captured_at:
                lines.append(f"  Date: {s.captured_at}")

        index_result = "\n".join(lines)

        # Append graph context if available
        if self._graph_retriever is not None:
            try:
                graph_context = await self._graph_retriever.retrieve(query, top_k=top_k)
                if graph_context:
                    return index_result + "\n\n" + graph_context
            except (FileNotFoundError, OSError, ValueError):
                logger.debug("graph_search_failed", exc_info=True)

        return index_result

    async def _vector_search(
        self,
        query: str,
        context_dirs: list[str] | None,
        top_k: int,
    ) -> str:
        """Semantic search using vector embeddings."""
        if self._vector_store is None or self._embedder is None:
            raise RuntimeError("Vector search requires vector_store and embedder")

        query_embedding = await self._embedder.embed(query)
        # Fetch more than top_k to allow directory filtering
        results = await self._vector_store.search(query_embedding, top_k=top_k * 3)

        if context_dirs:
            results = [
                (path, score)
                for path, score in results
                if any(path.startswith(d) for d in context_dirs)
            ]

        results = results[:top_k]

        if not results:
            return "No notes found."

        # Enrich with index summaries if available
        summary_map: dict[str, NoteSummary] = {}
        if self._index_reader is not None:
            all_summaries = await self._index_reader.get_all_summaries()
            summary_map = {s.path: s for s in all_summaries}

        lines = [f"Found {len(results)} notes by semantic similarity (query: {query}):"]
        lines.append("")
        for path, score in results:
            summary = summary_map.get(path)
            title = summary.title if summary else path
            lines.append(f"- **{title}** ({path}) [similarity: {score:.3f}]")
            if summary:
                if summary.tags:
                    lines.append(f"  Tags: {', '.join(f'#{t}' for t in summary.tags)}")
                if summary.captured_at:
                    lines.append(f"  Date: {summary.captured_at}")

        search_result = "\n".join(lines)

        # Append graph context if available
        if self._graph_retriever is not None:
            try:
                graph_context = await self._graph_retriever.retrieve(query, top_k=top_k)
                if graph_context:
                    return search_result + "\n\n" + graph_context
            except (FileNotFoundError, OSError, ValueError):
                logger.debug("graph_search_failed", exc_info=True)

        return search_result

    async def _index_retrieve(
        self,
        context_dirs: list[str],
        max_chars: int,
        top_k: int,
    ) -> str:
        """Retrieve using index: return summaries + recent full notes."""
        if self._index_reader is None:
            return await self._fallback_retrieve(context_dirs, max_chars, top_k)

        all_summaries: list[NoteSummary] = []
        for d in context_dirs:
            all_summaries.extend(await self._index_reader.get_summaries(d))

        if not all_summaries:
            logger.debug("index_no_summaries_fallback", dirs=context_dirs)
            return await self._fallback_retrieve(context_dirs, max_chars, top_k)

        # Sort by date (most recent first)
        all_summaries.sort(key=lambda s: s.captured_at or "0000-00-00", reverse=True)
        all_summaries = all_summaries[:top_k]

        # Build context: index listing + recent note contents
        parts: list[str] = []
        total = 0

        # Part 1: Index summary table
        index_header = "## Note Index\n"
        index_lines = [index_header]
        index_lines.append("| Title | Tags | Date |")
        index_lines.append("|-------|------|------|")
        for s in all_summaries:
            tags_str = ", ".join(f"#{t}" for t in s.tags) if s.tags else ""
            index_lines.append(f"| [[{s.title}]] | {tags_str} | {s.captured_at} |")
        index_text = "\n".join(index_lines)
        parts.append(index_text)
        total += len(index_text)

        # Part 2: Full content of most recent notes (fit within max_chars)
        recent = all_summaries[:5]  # Read up to 5 most recent full notes
        for s in recent:
            if total >= max_chars:
                break
            try:
                path = self._vault.resolve_path(s.path)
                content = await self._vault.read_note_content(path)
                remaining = max_chars - total
                parts.append(content[:remaining])
                total += len(parts[-1])
            except (FileNotFoundError, OSError, UnicodeDecodeError):
                logger.debug("index_read_note_failed", path=s.path)

        logger.info(
            "index_retrieve",
            count=len(all_summaries),
            total_chars=total,
        )
        return "\n---\n".join(parts)

    async def _fallback_retrieve(
        self,
        context_dirs: list[str],
        max_chars: int,
        max_notes_per_dir: int,
    ) -> str:
        """Recency-based retrieval fallback."""
        parts: list[str] = []
        total = 0

        for subdir in context_dirs:
            if total >= max_chars:
                break
            try:
                note_paths = await self._vault.read_notes(subdir)
            except (FileNotFoundError, OSError):
                continue
            for path in reversed(note_paths[-max_notes_per_dir:]):
                if total >= max_chars:
                    break
                try:
                    text = await self._vault.read_note_content(path)
                    remaining = max_chars - total
                    parts.append(text[:remaining])
                    total += len(parts[-1])
                except (FileNotFoundError, OSError, UnicodeDecodeError):
                    pass

        return "\n---\n".join(parts)

    async def reindex_all(self) -> int:
        """Full reindex of vault notes via FileIndexReader."""
        if self._index_reader is None:
            raise RuntimeError("Index reader not configured")
        await self._index_reader.rebuild_all()
        summaries = await self._index_reader.get_all_summaries()
        logger.info("reindex_complete", total=len(summaries))
        return len(summaries)
