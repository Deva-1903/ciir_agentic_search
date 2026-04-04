"""Tests for fuzzy entity deduplication logic."""

import pytest
from app.utils.dedupe import domains_match, find_matching_entity_idx, names_are_similar


class TestNamesAreSimilar:
    def test_identical(self):
        assert names_are_similar("OpenAI", "OpenAI")

    def test_case_insensitive(self):
        assert names_are_similar("openai", "OpenAI")

    def test_suffix_stripped(self):
        assert names_are_similar("OpenAI Inc", "OpenAI")

    def test_reorder_tokens(self):
        assert names_are_similar("Google DeepMind", "DeepMind Google")

    def test_clearly_different(self):
        assert not names_are_similar("Apple", "Microsoft")

    def test_similar_with_legal_suffix(self):
        # "Acme Corp" vs "Acme" — should match
        assert names_are_similar("Acme Corp", "Acme")


class TestDomainsMatch:
    def test_same_domain(self):
        assert domains_match("https://example.com/page", "https://example.com/other")

    def test_different_domains(self):
        assert not domains_match("https://alpha.com", "https://beta.com")

    def test_www_variant(self):
        assert domains_match("https://www.example.com", "https://example.com")

    def test_empty_url(self):
        assert not domains_match("", "https://example.com")


class TestFindMatchingEntityIdx:
    def test_finds_by_name(self):
        existing = [
            {"name": "Stripe", "website": None},
            {"name": "Plaid", "website": None},
        ]
        idx = find_matching_entity_idx("Stripe Inc", None, existing)
        assert idx == 0

    def test_finds_by_domain(self):
        existing = [
            {"name": "TechCorp", "website": "https://techcorp.com"},
        ]
        idx = find_matching_entity_idx("TechCorp Ltd", "https://techcorp.com/about", existing)
        assert idx == 0

    def test_returns_none_on_no_match(self):
        existing = [{"name": "Alpha", "website": None}]
        idx = find_matching_entity_idx("Beta Corp", None, existing)
        assert idx is None

    def test_empty_existing(self):
        idx = find_matching_entity_idx("Anything", None, [])
        assert idx is None
