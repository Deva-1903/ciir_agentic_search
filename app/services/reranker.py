"""
Query-aware reranker for scraped pages, used before extraction.

Brave + URL-dedup gives us ~15–25 candidate pages. Running the LLM extractor on
every one wastes budget on pages that are loosely related to the query. This
module scores each (query, page) pair with a cross-encoder and keeps the top-K.

Primary scorer: `cross-encoder/ms-marco-MiniLM-L-6-v2` via sentence-transformers.
  - Loaded lazily on first use; model load cost (~2–4s) is paid once per process.
  - Each (query, page) pair: ~20–80ms on CPU for a 512-token input.

Fallback scorer: lexical token overlap (Jaccard). Used when:
  - sentence-transformers is not installed,
  - the model fails to load (e.g. no network on first run, disk full),
  - the cross-encoder itself raises at score time.

The fallback is deliberately simple and deterministic so the pipeline stays
runnable even in constrained environments.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from app.core.logging import get_logger
from app.models.schema import ScrapedPage

log = get_logger(__name__)

# Tunables
_MAX_PAGE_CHARS_FOR_SCORING = 1200   # leading slice of cleaned text used as the "document"
_CROSS_ENCODER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Module-level model handle; populated on first successful load.
# `False` means load was attempted and failed — do not retry in this process.
_model: object | None = None
_model_load_failed: bool = False


def _load_model_if_needed() -> Optional[object]:
    """Lazy-load the cross-encoder. Returns the model or None if unavailable."""
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        return None
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except Exception as exc:
        log.warning("sentence-transformers not importable (%s); using lexical fallback", exc)
        _model_load_failed = True
        return None
    try:
        _model = CrossEncoder(_CROSS_ENCODER_MODEL_NAME, max_length=512)
        log.info("Loaded cross-encoder %s", _CROSS_ENCODER_MODEL_NAME)
    except Exception as exc:
        log.warning("CrossEncoder load failed (%s); using lexical fallback", exc)
        _model_load_failed = True
        return None
    return _model


# ── Scorers ───────────────────────────────────────────────────────────────────

def _page_doc(page: ScrapedPage) -> str:
    """Build the text used to represent a page for scoring."""
    title = page.title or ""
    head = (page.cleaned_text or "")[:_MAX_PAGE_CHARS_FOR_SCORING]
    return f"{title}\n{head}".strip()


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _lexical_score(query: str, doc: str) -> float:
    """Jaccard overlap between query tokens and doc tokens. Range [0, 1]."""
    q = _tokens(query)
    d = _tokens(doc)
    if not q or not d:
        return 0.0
    inter = q & d
    return len(inter) / len(q)  # recall of query terms in document


def _cross_encoder_scores(
    model, query: str, docs: list[str]
) -> Optional[list[float]]:
    """Score all (query, doc) pairs in a single batch. Returns None on failure."""
    if not docs:
        return []
    try:
        pairs = [(query, d) for d in docs]
        raw = model.predict(pairs, show_progress_bar=False)
        return [float(x) for x in raw]
    except Exception as exc:
        log.warning("CrossEncoder.predict failed (%s); using lexical fallback", exc)
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

async def rerank_pages(
    query: str,
    pages: list[ScrapedPage],
    top_k: int,
) -> tuple[list[ScrapedPage], dict]:
    """
    Score pages against the query, return the top_k highest-scoring pages.

    Returns:
      (ranked_pages, info_dict)
    where info_dict carries observability fields:
      - scorer: "cross_encoder" | "lexical"
      - pages_before: int
      - pages_after: int
      - top_scores: list[float]  (scores of the returned pages, same order)
    """
    if not pages:
        return [], {"scorer": "none", "pages_before": 0, "pages_after": 0, "top_scores": []}

    docs = [_page_doc(p) for p in pages]
    model = _load_model_if_needed()

    scorer_name = "lexical"
    scores: Optional[list[float]] = None

    if model is not None:
        # model.predict is sync + CPU-bound; push to a worker thread to avoid
        # blocking the event loop.
        scores = await asyncio.to_thread(_cross_encoder_scores, model, query, docs)
        if scores is not None:
            scorer_name = "cross_encoder"

    if scores is None:
        scores = [_lexical_score(query, d) for d in docs]

    ranked = sorted(zip(pages, scores), key=lambda pair: pair[1], reverse=True)
    kept = ranked[: max(1, top_k)]
    kept_pages = [p for p, _ in kept]
    kept_scores = [round(s, 4) for _, s in kept]

    log.info(
        "Rerank (%s): kept %d/%d pages; top scores=%s",
        scorer_name,
        len(kept_pages),
        len(pages),
        kept_scores[:5],
    )

    return kept_pages, {
        "scorer": scorer_name,
        "pages_before": len(pages),
        "pages_after": len(kept_pages),
        "top_scores": kept_scores,
    }
