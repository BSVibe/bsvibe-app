"""Multimodal extraction — extract entities from PDFs, images, and audio.

Routes non-markdown files to appropriate extractors:
- PDF: pypdf text extraction → LLM entity extraction
- Image: LLM vision API → semantic description → entity extraction
- Audio/Video: faster-whisper transcription → document extraction

All extracted entities and relationships are saved as markdown notes
in the vault, maintaining the "human-readable ontology" principle.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

import structlog

from backend.knowledge.graph.graph_models import ConfidenceLevel, GraphEntity, GraphRelationship
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)


class MediaType(StrEnum):
    """Supported media file types."""

    MARKDOWN = "markdown"
    PDF = "pdf"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    UNKNOWN = "unknown"


_EXT_TO_MEDIA: dict[str, MediaType] = {
    ".md": MediaType.MARKDOWN,
    ".pdf": MediaType.PDF,
    ".png": MediaType.IMAGE,
    ".jpg": MediaType.IMAGE,
    ".jpeg": MediaType.IMAGE,
    ".gif": MediaType.IMAGE,
    ".webp": MediaType.IMAGE,
    ".bmp": MediaType.IMAGE,
    ".mp3": MediaType.AUDIO,
    ".wav": MediaType.AUDIO,
    ".m4a": MediaType.AUDIO,
    ".flac": MediaType.AUDIO,
    ".ogg": MediaType.AUDIO,
    ".mp4": MediaType.VIDEO,
    ".mov": MediaType.VIDEO,
    ".avi": MediaType.VIDEO,
    ".mkv": MediaType.VIDEO,
}


def classify_media(rel_path: str) -> MediaType:
    """Classify a file by its extension."""
    ext = PurePosixPath(rel_path).suffix.lower()
    return _EXT_TO_MEDIA.get(ext, MediaType.UNKNOWN)


@dataclass
class ExtractionResult:
    """Result from multimodal extraction."""

    source_path: str
    media_type: MediaType
    extracted_text: str
    entities: list[GraphEntity]
    relationships: list[GraphRelationship]
    note_content: str  # markdown note content to save to vault


TextExtractor = Callable[[bytes], Awaitable[str]]
ImageExtractor = Callable[[bytes, str], Awaitable[str]]  # (image_bytes, prompt) → description
LLMExtractor = Callable[[str, str], Awaitable[tuple[list[GraphEntity], list[GraphRelationship]]]]


async def extract_pdf_text(data: bytes) -> str:
    """Extract text from a PDF. Requires ``pypdf`` dependency.

    Falls back to empty string if pypdf is not installed. Callers should
    handle this gracefully.
    """
    try:
        from io import BytesIO

        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("pypdf_not_installed")
        return ""

    reader = PdfReader(BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


async def extract_image_description(
    data: bytes,
    image_llm_fn: ImageExtractor,
    prompt: str | None = None,
) -> str:
    """Ask a vision LLM to describe an image semantically."""
    default_prompt = (
        "Describe this image as a knowledge graph entity. Identify key concepts, "
        "diagrams, entities, and relationships visible. Be specific. "
        "Do not just OCR — extract semantic meaning."
    )
    return await image_llm_fn(data, prompt or default_prompt)


def _make_note(rel_path: str, media_type: MediaType, text: str) -> tuple[str, str]:
    """Generate a markdown note for extracted media content.

    Returns (note_path, note_content).
    """
    stem = PurePosixPath(rel_path).stem
    digest = hashlib.sha256(rel_path.encode()).hexdigest()[:8]
    note_path = f"garden/extracted/{media_type.value}-{stem}-{digest}.md"

    frontmatter = (
        f"---\n"
        f"type: extracted\n"
        f"media_type: {media_type.value}\n"
        f"source_file: {rel_path}\n"
        f"confidence: {ConfidenceLevel.INFERRED.value}\n"
        f"---\n\n"
    )
    body = f"# Extracted from `{rel_path}`\n\n{text}\n"
    return note_path, frontmatter + body


async def extract_from_media(
    rel_path: str,
    data: bytes,
    *,
    llm_extractor: LLMExtractor | None = None,
    image_llm_fn: ImageExtractor | None = None,
) -> ExtractionResult:
    """Route a media file to its appropriate extractor.

    Returns ExtractionResult with extracted text and entities (if an LLM
    extractor is provided).
    """
    media_type = classify_media(rel_path)
    text = ""

    if media_type == MediaType.PDF:
        text = await extract_pdf_text(data)
    elif media_type == MediaType.IMAGE and image_llm_fn is not None:
        text = await extract_image_description(data, image_llm_fn)
    elif media_type in (MediaType.AUDIO, MediaType.VIDEO):
        logger.info("audio_video_transcription_not_implemented", path=rel_path)
        text = ""

    entities: list[GraphEntity] = []
    relationships: list[GraphRelationship] = []
    if text and llm_extractor is not None:
        entities, relationships = await llm_extractor(rel_path, text)

    note_path, note_content = _make_note(rel_path, media_type, text)

    logger.info(
        "multimodal_extracted",
        path=rel_path,
        media_type=media_type.value,
        text_length=len(text),
        entities=len(entities),
        relationships=len(relationships),
    )

    return ExtractionResult(
        source_path=note_path,
        media_type=media_type,
        extracted_text=text,
        entities=entities,
        relationships=relationships,
        note_content=note_content,
    )


async def write_extraction_note(
    storage: StorageBackend,
    result: ExtractionResult,
) -> str:
    """Write the extraction result as a markdown note via storage.

    Returns the relative path of the written note.
    """
    await storage.write(result.source_path, result.note_content)
    return result.source_path
