"""Text processing utilities: chunking, cleaning, token estimation."""

from __future__ import annotations

import re
import unicodedata


def clean_text(text: str) -> str:
    """Normalize whitespace and remove junk characters."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\x20-\x7E\u00A0-\uFFFF]", " ", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    """Fast token estimate: ~4 chars per token (good enough for chunking)."""
    return len(text) // 4


def chunk_text(
    text: str,
    max_tokens: int = 3500,
    overlap_tokens: int = 100,
    max_chunks: int | None = None,
) -> list[str]:
    """
    Split text into chunks of roughly max_tokens, with a small word-boundary
    overlap to avoid cutting mid-entity.  Returns [] if text is very short.
    """
    if estimate_tokens(text) <= max_tokens:
        return [text]

    chars_per_chunk = max_tokens * 4
    overlap_chars = overlap_tokens * 4
    chunks: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + chars_per_chunk, length)
        # Try to break at a sentence boundary
        if end < length:
            boundary = text.rfind(". ", start, end)
            if boundary != -1 and boundary > start + chars_per_chunk // 2:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if max_chunks is not None and len(chunks) >= max_chunks:
            break
        if end >= length:
            break

        # Always advance, even if overlap settings are too aggressive.
        start = max(end - overlap_chars, start + 1)

    return [c for c in chunks if c]


def truncate(text: str, max_chars: int = 300) -> str:
    """Truncate to max_chars, ending at a word boundary."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    return (truncated[:last_space] if last_space > 0 else truncated) + "…"


def normalize_name(name: str) -> str:
    """Lowercase, remove punctuation, collapse spaces — used for fuzzy matching."""
    name = name.lower()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name
