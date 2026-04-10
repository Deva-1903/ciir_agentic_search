"""Lightweight query normalization for safer planning and retrieval."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedQuery:
    original_query: str
    normalized_query: str


_SAFE_TOKEN_FIXES = {
    "restraunts": "restaurants",
    "resturants": "restaurants",
    "resturant": "restaurant",
    "restraunt": "restaurant",
    "cofee": "coffee",
    "coffe": "coffee",
    "ramenn": "ramen",
    "softwre": "software",
    "databse": "database",
    "datbases": "databases",
    "compaines": "companies",
    "brookln": "Brooklyn",
    "brookyln": "Brooklyn",
    "newyork": "New York",
    "losangeles": "Los Angeles",
    "seattel": "Seattle",
    "lisbonn": "Lisbon",
    "denvar": "Denver",
    "londn": "London",
    "amhersrt": "Amherst",
}

_STATE_ABBREVIATIONS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
    "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
    "dc",
}

_KNOWN_LOCATION_TERMS = {
    "Amherst",
    "Brooklyn",
    "Colorado",
    "Denver",
    "Lisbon",
    "London",
    "Los Angeles",
    "Massachusetts",
    "New York",
    "Portugal",
    "Seattle",
    "United States",
}

_LOCATION_PREPOSITIONS = {"in", "near", "around", "at"}
_AMBIGUOUS_STATE_ABBREVIATIONS = {"hi", "in", "me", "ok", "or"}


def _cleanup_punctuation(query: str) -> str:
    query = re.sub(r"[!?]{2,}", "?", query)
    query = re.sub(r"\.{2,}", ".", query)
    query = re.sub(r",{2,}", ",", query)
    query = re.sub(r"\s*,\s*", ", ", query)
    query = re.sub(r"\s+", " ", query)
    return query.strip(" ,.;:-")


def _tokenize(query: str) -> list[str]:
    # it keeps apostrophes in contractions like "don't",
    # keeps commas as separate tokens (for location phrases like "Brooklyn, NY"), and discards everything else.
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[0-9]+|[,]", query)


def _retokenize(tokens: list[str]) -> str:
    text = " ".join(tokens)
    text = text.replace(" ,", ",")
    return re.sub(r"\s+", " ", text).strip()


def _maybe_fix_location_token(
    token: str,
    prev_token: str | None,
    next_token: str | None,
) -> str:
    low = token.lower()
    if low in _SAFE_TOKEN_FIXES:
        return _SAFE_TOKEN_FIXES[low]

    # Only attempt fuzzy location correction in explicit location contexts.
    locationish = (
        (prev_token or "").lower() in _LOCATION_PREPOSITIONS
        or (next_token or "").lower() in _STATE_ABBREVIATIONS
    )
    if not locationish or len(token) < 5 or not token.isalpha():
        return token

    choices = {term.lower(): term for term in _KNOWN_LOCATION_TERMS if " " not in term}
    # more shared character structure = higher score
    # more different ordering / missing chars = lower score
    match = difflib.get_close_matches(low, list(choices.keys()), n=1, cutoff=0.84)
    if match:
        return choices[match[0]]
    return token


def _looks_like_state_abbreviation(tokens: list[str], idx: int) -> bool:
    token = tokens[idx]
    low = token.lower()
    if low not in _STATE_ABBREVIATIONS or len(token) > 3:
        return False

    if token.isupper():
        return True

    prev_token = tokens[idx - 1] if idx > 0 else None
    next_token = tokens[idx + 1] if idx + 1 < len(tokens) else None

    if low in _AMBIGUOUS_STATE_ABBREVIATIONS:
        if prev_token == "," or next_token == ",":
            return True
        if idx == len(tokens) - 1 and prev_token not in _LOCATION_PREPOSITIONS:
            return True
        return False

    return idx == len(tokens) - 1 or prev_token == "," or next_token == ","


def normalize_query(query: str) -> NormalizedQuery:
    """Return a lightly normalized query, preserving intent and only safe fixes."""
    original = query.strip()
    cleaned = _cleanup_punctuation(original)
    if not cleaned:
        return NormalizedQuery(original_query=original, normalized_query=original)

    # splits into a flat list of tokens — words, numbers, and commas only. Drops everything else.   
    raw_tokens = _tokenize(cleaned)
    # output - ["best", "pizza", "places", "in", "brookln", "ny"]
    normalized_tokens: list[str] = []
    for idx, token in enumerate(raw_tokens):
        if token == ",":
            normalized_tokens.append(token)
            continue

        prev_token = raw_tokens[idx - 1] if idx > 0 else None
        next_token = raw_tokens[idx + 1] if idx + 1 < len(raw_tokens) else None
        low = token.lower()

        if _looks_like_state_abbreviation(raw_tokens, idx):
            normalized_tokens.append(low.upper())
            continue

        if low in _SAFE_TOKEN_FIXES:
            normalized_tokens.append(_SAFE_TOKEN_FIXES[low])
            continue

        fixed = _maybe_fix_location_token(token, prev_token, next_token)
        if fixed != token:
            normalized_tokens.append(fixed)
            continue

        # Proper-case exact known locations while leaving the rest of the query alone.
        exact = next((term for term in _KNOWN_LOCATION_TERMS if term.lower() == low), None)
        normalized_tokens.append(exact or token)

    normalized = _retokenize(normalized_tokens)
    return NormalizedQuery(original_query=original, normalized_query=normalized or original)
