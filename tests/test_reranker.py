"""Tests for the reranker: scoring, top-K selection, fallback behavior."""

from __future__ import annotations

import pytest

from app.models.schema import ScrapedPage
from app.services import reranker


def _page(url: str, title: str, text: str) -> ScrapedPage:
    return ScrapedPage(url=url, title=title, cleaned_text=text)


def test_lexical_score_gives_higher_score_to_topical_doc():
    q = "pizza places brooklyn"
    topical = "the best pizza places in brooklyn, ranked by critics"
    off_topic = "how to fix a leaky faucet in three steps"
    assert reranker._lexical_score(q, topical) > reranker._lexical_score(q, off_topic)


def test_lexical_score_handles_empty_strings():
    assert reranker._lexical_score("", "anything") == 0.0
    assert reranker._lexical_score("query", "") == 0.0


def test_page_doc_combines_title_and_leading_text():
    page = _page("https://x.com", "Title Line", "Body content goes here")
    doc = reranker._page_doc(page)
    assert "Title Line" in doc
    assert "Body content goes here" in doc


def test_page_doc_truncates_long_text():
    long_body = "word " * 2000
    page = _page("https://x.com", "T", long_body)
    doc = reranker._page_doc(page)
    # title (1) + newline (1) + truncated body (≤ _MAX_PAGE_CHARS_FOR_SCORING)
    assert len(doc) <= reranker._MAX_PAGE_CHARS_FOR_SCORING + 10


@pytest.mark.asyncio
async def test_rerank_keeps_top_k_ordered_by_score(monkeypatch):
    # Force lexical fallback to make the test deterministic and fast.
    monkeypatch.setattr(reranker, "_load_model_if_needed", lambda: None)

    pages = [
        _page("https://a.com", "unrelated", "talking about cats and dogs"),
        _page("https://b.com", "brooklyn pizza guide", "best pizza places in brooklyn neighborhoods"),
        _page("https://c.com", "half match", "pizza delivery services"),
    ]
    kept, info = await reranker.rerank_pages("pizza places brooklyn", pages, top_k=2)

    assert len(kept) == 2
    assert kept[0].url == "https://b.com"  # highest topical overlap
    assert info["scorer"] == "lexical"
    assert info["pages_before"] == 3
    assert info["pages_after"] == 2
    assert len(info["top_scores"]) == 2
    assert info["top_scores"][0] >= info["top_scores"][1]


@pytest.mark.asyncio
async def test_rerank_empty_pages_returns_empty(monkeypatch):
    monkeypatch.setattr(reranker, "_load_model_if_needed", lambda: None)
    kept, info = await reranker.rerank_pages("any query", [], top_k=5)
    assert kept == []
    assert info["pages_before"] == 0
    assert info["pages_after"] == 0


@pytest.mark.asyncio
async def test_rerank_falls_back_when_cross_encoder_raises(monkeypatch):
    class BrokenModel:
        def predict(self, pairs, show_progress_bar=False):
            raise RuntimeError("model crashed")

    monkeypatch.setattr(reranker, "_load_model_if_needed", lambda: BrokenModel())
    pages = [
        _page("https://a.com", "pizza brooklyn", "great pizza in brooklyn neighborhoods"),
        _page("https://b.com", "random", "random content unrelated"),
    ]
    kept, info = await reranker.rerank_pages("pizza brooklyn", pages, top_k=1)
    assert len(kept) == 1
    assert info["scorer"] == "lexical"  # fell back


@pytest.mark.asyncio
async def test_rerank_top_k_larger_than_input_returns_all(monkeypatch):
    monkeypatch.setattr(reranker, "_load_model_if_needed", lambda: None)
    pages = [_page(f"https://{i}.com", f"t{i}", "text") for i in range(3)]
    kept, info = await reranker.rerank_pages("query", pages, top_k=10)
    assert len(kept) == 3
    assert info["pages_after"] == 3
