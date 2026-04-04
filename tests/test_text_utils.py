"""Tests for text utilities: chunking, token estimation, normalization."""

import pytest
from app.utils.text import chunk_text, estimate_tokens, normalize_name, truncate


class TestEstimateTokens:
    def test_short_text(self):
        assert estimate_tokens("hello world") < 10

    def test_longer_text(self):
        text = "word " * 1000
        est = estimate_tokens(text)
        assert 900 < est < 1500  # rough estimate


class TestChunkText:
    def test_short_text_returns_single_chunk(self):
        text = "Short text that fits easily."
        chunks = chunk_text(text, max_tokens=500)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_splits_into_multiple(self):
        # ~14000 chars ≈ 3500 tokens → should split
        text = "word sentence. " * 1000
        chunks = chunk_text(text, max_tokens=500)
        assert len(chunks) > 1

    def test_all_content_preserved(self):
        text = "alpha beta gamma delta. " * 500
        chunks = chunk_text(text, max_tokens=300)
        combined = " ".join(chunks)
        # Check key content present (with possible overlap, words are present)
        assert "alpha" in combined
        assert "gamma" in combined

    def test_chunks_not_empty(self):
        text = "sentence. " * 400
        chunks = chunk_text(text, max_tokens=200)
        assert all(len(c) > 0 for c in chunks)

    def test_long_text_does_not_loop_forever_on_final_chunk(self):
        text = "word sentence. " * 2000
        chunks = chunk_text(text, max_tokens=500, overlap_tokens=100)

        assert len(chunks) > 1
        assert len(chunks) < 20

    def test_respects_max_chunks_limit(self):
        text = "word sentence. " * 2000
        chunks = chunk_text(text, max_tokens=200, max_chunks=2)

        assert len(chunks) == 2


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("hello", max_chars=100) == "hello"

    def test_truncates_long_text(self):
        text = "word " * 100
        result = truncate(text, max_chars=50)
        assert len(result) <= 52  # +ellipsis

    def test_ends_with_ellipsis(self):
        text = "a " * 100
        result = truncate(text, max_chars=20)
        assert result.endswith("…")


class TestNormalizeName:
    def test_lowercase(self):
        assert normalize_name("OpenAI") == "openai"

    def test_strips_punctuation(self):
        assert normalize_name("Corp. Inc!") == "corp inc"

    def test_collapses_spaces(self):
        assert normalize_name("  A   B  ") == "a b"
