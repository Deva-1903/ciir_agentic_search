"""Tests for page evidence-regime detection."""

from app.services.evidence_regimes import classify_page_evidence, page_likely_needs_js


def test_classify_page_evidence_detects_software_repo_or_docs():
    regime, confidence = classify_page_evidence(
        "https://github.com/langchain-ai/langchain",
        title="langchain-ai/langchain: Build context-aware reasoning apps - GitHub",
        cleaned_text="LangChain is a framework for building applications powered by language models.",
        metadata={"json_ld_types": [], "script_count": 3},
    )

    assert regime == "software_repo_or_docs"
    assert confidence >= 0.9


def test_classify_page_evidence_detects_local_business_listing_from_structured_data():
    regime, confidence = classify_page_evidence(
        "https://lucali.com/contact",
        title="Contact Lucali",
        cleaned_text="Lucali 575 Henry St Brooklyn NY 11231. Call 718-858-4086.",
        metadata={
            "json_ld_types": ["Restaurant", "LocalBusiness"],
            "structured_data": [
                {
                    "@type": ["Restaurant", "LocalBusiness"],
                    "name": "Lucali",
                    "telephone": "718-858-4086",
                    "address": "575 Henry St, Brooklyn, NY 11231",
                }
            ],
            "script_count": 2,
        },
    )

    assert regime == "local_business_listing"
    assert confidence >= 0.75


def test_page_likely_needs_js_detects_app_shell():
    html = """
    <html>
      <head><script>window.__INITIAL_STATE__={}</script></head>
      <body><div id="__next">Loading...</div></body>
    </html>
    """
    assert page_likely_needs_js(html, "", {"script_count": 12}, min_text_length=200) is True
