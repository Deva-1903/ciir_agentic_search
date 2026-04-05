"""Deterministic and semi-deterministic page parsers used before the LLM."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.models.schema import CellDraft, EntityDraft, PlannerOutput, ScrapedPage
from app.services.field_validator import validate_and_normalize
from app.utils.text import clean_text, truncate
from app.utils.url import extract_domain, is_useful_url

_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\(\) ]{7,}\d)")
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[a-z0-9][a-z0-9 .'-]{2,80}\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct)\b[^.;]{0,50}",
    re.IGNORECASE,
)
_LICENSE_RE = re.compile(
    r"\b(?:mit|apache(?:\s+2\.0)?|bsd(?:\s+\d-clause)?|gpl(?:v?\d(?:\.\d)?)?|lgpl|mpl|agpl)\b",
    re.IGNORECASE,
)
_NAV_LINK_TEXT = {
    "about",
    "account",
    "blog",
    "careers",
    "contact",
    "docs",
    "documentation",
    "features",
    "help",
    "home",
    "jobs",
    "learn more",
    "login",
    "pricing",
    "privacy",
    "read more",
    "resources",
    "search",
    "sign in",
    "sign up",
    "support",
    "terms",
}
_LOCAL_TYPES = {
    "bakery",
    "barorspub",
    "cafetorcoffeeshop",
    "foodestablishment",
    "hotel",
    "localbusiness",
    "place",
    "restaurant",
    "store",
}
_COMMON_LANGUAGES = (
    "python",
    "typescript",
    "javascript",
    "go",
    "rust",
    "java",
    "kotlin",
    "swift",
    "ruby",
    "php",
    "c++",
    "c#",
)


def _canonical_homepage(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    return f"{scheme}://{parsed.netloc}/" if parsed.netloc else url


def _repo_root(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return f"{parsed.scheme or 'https'}://{parsed.netloc}/{parts[0]}/{parts[1]}"
    return _canonical_homepage(url)


def _meta_description(page: ScrapedPage) -> str:
    return clean_text(str((page.page_metadata or {}).get("meta_description") or ""))


def _structured_items(page: ScrapedPage) -> list[dict[str, Any]]:
    items = (page.page_metadata or {}).get("structured_data") or []
    return [item for item in items if isinstance(item, dict)]


def _structured_item_name(page: ScrapedPage) -> str:
    for item in _structured_items(page):
        name = clean_text(str(item.get("name") or ""))
        if name:
            return name
    return ""


def _title_guess(title: str) -> str:
    if not title:
        return ""
    for delimiter in (" | ", " - ", " :: ", " — ", ": "):
        if delimiter in title:
            head = clean_text(title.split(delimiter, 1)[0])
            if head:
                return head
    return clean_text(title)


def _repo_name_from_url(page: ScrapedPage) -> tuple[str, str]:
    parsed = urlparse(page.url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    domain = extract_domain(page.url)
    return "", domain.split(".")[0] if domain else ""


def _guess_entity_name(page: ScrapedPage) -> str:
    structured_name = _structured_item_name(page)
    if structured_name:
        return structured_name

    headings = (page.page_metadata or {}).get("headings") or []
    if headings:
        return clean_text(str(headings[0]))

    title_name = _title_guess(page.title)
    if title_name:
        return title_name

    owner, repo = _repo_name_from_url(page)
    if repo:
        return repo
    return owner or extract_domain(page.url)


def _first_sentence(text: str) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    for sep in (". ", "! ", "? "):
        if sep in cleaned:
            return cleaned.split(sep, 1)[0].strip()
    return cleaned[:180].strip()


def _supporting_snippet(page: ScrapedPage, needle: str | None, fallback: str) -> str:
    text = page.cleaned_text or ""
    if needle:
        lower = text.lower()
        target = needle.lower()
        idx = lower.find(target)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(text), idx + len(needle) + 80)
            return truncate(clean_text(text[start:end]), 200)
    return truncate(clean_text(fallback or page.title or text[:140]), 200)


def _add_cell(
    cells: dict[str, CellDraft],
    plan: PlannerOutput,
    page: ScrapedPage,
    col: str,
    value: str | None,
    *,
    evidence_hint: str | None = None,
    confidence: float = 0.8,
) -> None:
    if col not in plan.columns or not value:
        return
    normalized, ok = validate_and_normalize(
        col,
        value,
        source_url=page.url,
        source_title=page.title or None,
    )
    if not ok:
        return
    snippet = _supporting_snippet(page, evidence_hint or value, evidence_hint or value)
    cells[col] = CellDraft(
        value=normalized,
        evidence_snippet=snippet,
        confidence=confidence,
    )


def _entity_draft(name: str, cells: dict[str, CellDraft], page: ScrapedPage) -> EntityDraft | None:
    clean_name = clean_text(name)
    if not clean_name:
        return None
    if "name" not in cells:
        cells["name"] = CellDraft(
            value=clean_name,
            evidence_snippet=truncate(clean_name, 160),
            confidence=0.82,
        )
    return EntityDraft(
        entity_name=clean_name,
        cells=cells,
        source_url=page.url,
        source_title=page.title or None,
    )


def _find_language(text: str) -> str:
    lower = text.lower()
    for language in _COMMON_LANGUAGES:
        if language in lower:
            return language
    return ""


def _extract_phone(page: ScrapedPage) -> str:
    metadata = page.page_metadata or {}
    tel_links = metadata.get("tel_links") or []
    if tel_links:
        return clean_text(str(tel_links[0]))
    match = _PHONE_RE.search(page.cleaned_text or "")
    return clean_text(match.group(0)) if match else ""


def _extract_address(page: ScrapedPage) -> str:
    for item in _structured_items(page):
        address = clean_text(str(item.get("address") or ""))
        if address:
            return address
    match = _ADDRESS_RE.search(page.cleaned_text or "")
    return clean_text(match.group(0)) if match else ""


def _extract_local_business(page: ScrapedPage, plan: PlannerOutput) -> list[EntityDraft]:
    items = _structured_items(page)
    selected: dict[str, Any] | None = None
    for item in items:
        types = {str(t).lower() for t in item.get("@type", [])}
        if types & _LOCAL_TYPES or item.get("telephone") or item.get("address"):
            selected = item
            break

    if selected is None:
        return []

    name = clean_text(str(selected.get("name") or _guess_entity_name(page)))
    if not name:
        return []

    cells: dict[str, CellDraft] = {}
    _add_cell(cells, plan, page, "name", name, confidence=0.9)
    homepage = selected.get("url") or _canonical_homepage(page.url)
    for website_col in ("website", "url", "homepage", "site", "website_or_profile"):
        _add_cell(cells, plan, page, website_col, homepage, evidence_hint=homepage, confidence=0.9)
    address = clean_text(str(selected.get("address") or ""))
    for location_col in ("location", "address", "headquarters"):
        _add_cell(cells, plan, page, location_col, address, evidence_hint=address, confidence=0.86)
    phone = clean_text(str(selected.get("telephone") or _extract_phone(page)))
    for contact_col in ("phone", "phone_number", "telephone", "contact_or_booking"):
        _add_cell(cells, plan, page, contact_col, phone, evidence_hint=phone, confidence=0.84)
    category = clean_text(str(selected.get("servesCuisine") or ""))
    if not category:
        types = [t for t in selected.get("@type", []) if str(t).lower() not in _LOCAL_TYPES]
        category = clean_text(", ".join(str(t) for t in types[:2]))
    _add_cell(cells, plan, page, "category", category, evidence_hint=category, confidence=0.74)
    offering = clean_text(str(selected.get("description") or selected.get("servesCuisine") or ""))
    _add_cell(cells, plan, page, "offering", offering, evidence_hint=offering, confidence=0.72)
    _add_cell(cells, plan, page, "price_or_availability", clean_text(str(selected.get("priceRange") or selected.get("offers") or "")), confidence=0.7)

    draft = _entity_draft(name, cells, page)
    return [draft] if draft else []


def _extract_software_repo_or_docs(page: ScrapedPage, plan: PlannerOutput) -> list[EntityDraft]:
    owner, repo = _repo_name_from_url(page)
    name = _guess_entity_name(page)
    if repo and repo.lower() in page.title.lower():
        name = repo
    name = clean_text(name)
    if not name:
        return []

    cells: dict[str, CellDraft] = {}
    _add_cell(cells, plan, page, "name", name, confidence=0.9)

    repo_root = _repo_root(page.url)
    for website_col in ("website_or_repo", "website", "url", "homepage", "site"):
        _add_cell(cells, plan, page, website_col, repo_root, evidence_hint=repo_root, confidence=0.92)

    _add_cell(cells, plan, page, "maintainer_or_org", owner, evidence_hint=owner, confidence=0.82)

    license_match = _LICENSE_RE.search(page.cleaned_text or "")
    license_text = clean_text(license_match.group(0)) if license_match else ""
    _add_cell(cells, plan, page, "license", license_text, evidence_hint=license_text, confidence=0.78)

    language = _find_language(f"{page.title} {_meta_description(page)} {page.cleaned_text[:600]}")
    _add_cell(cells, plan, page, "language_or_stack", language, evidence_hint=language, confidence=0.74)

    use_case = _meta_description(page) or _first_sentence(page.cleaned_text)
    _add_cell(cells, plan, page, "primary_use_case", use_case, evidence_hint=use_case, confidence=0.7)
    _add_cell(cells, plan, page, "key_feature", use_case, evidence_hint=use_case, confidence=0.68)
    _add_cell(cells, plan, page, "description", use_case, evidence_hint=use_case, confidence=0.68)

    draft = _entity_draft(name, cells, page)
    return [draft] if draft else []


def _extract_official_page(page: ScrapedPage, plan: PlannerOutput) -> list[EntityDraft]:
    name = _guess_entity_name(page)
    if not name:
        return []

    cells: dict[str, CellDraft] = {}
    _add_cell(cells, plan, page, "name", name, confidence=0.86)
    homepage = _canonical_homepage(page.url)
    for website_col in ("website", "url", "homepage", "site", "website_or_profile", "website_or_repo"):
        _add_cell(cells, plan, page, website_col, homepage, evidence_hint=homepage, confidence=0.9)

    address = _extract_address(page)
    for location_col in ("location", "address", "headquarters"):
        _add_cell(cells, plan, page, location_col, address, evidence_hint=address, confidence=0.8)

    phone = _extract_phone(page)
    for contact_col in ("phone", "phone_number", "telephone", "contact_or_booking"):
        _add_cell(cells, plan, page, contact_col, phone, evidence_hint=phone, confidence=0.82)

    summary = _meta_description(page) or _first_sentence(page.cleaned_text)
    for summary_col in ("description", "overview", "summary", "focus_area", "product_or_service", "offering"):
        _add_cell(cells, plan, page, summary_col, summary, evidence_hint=summary, confidence=0.68)

    draft = _entity_draft(name, cells, page)
    return [draft] if draft else []


def _candidate_anchor_score(text: str, href: str, parent_text: str) -> float:
    score = 0.0
    lower = text.lower()
    if lower in _NAV_LINK_TEXT:
        return -10.0
    if len(text.split()) >= 2:
        score += 1.0
    if len(text) <= 40:
        score += 0.5
    if any(ch.isupper() for ch in text[:2]):
        score += 0.5
    if extract_domain(href):
        score += 0.25
    if parent_text and len(parent_text) > len(text):
        score += 0.25
    if re.fullmatch(r"[a-z0-9][a-z0-9 .,'&+\-]{1,79}", lower):
        score += 0.25
    if lower.startswith(("read ", "view ", "see ", "learn ")):
        score -= 1.0
    return score


def _extract_directory_candidates(page: ScrapedPage, plan: PlannerOutput) -> list[EntityDraft]:
    if not page.raw_html:
        return []
    try:
        soup = BeautifulSoup(page.raw_html, "lxml")
    except Exception:
        return []

    seen_names: set[str] = set()
    candidates: list[tuple[float, str, str, str]] = []

    for anchor in soup.find_all("a", href=True):
        text = clean_text(anchor.get_text(" ", strip=True))
        if not text or len(text) < 3 or len(text) > 80:
            continue
        href = urljoin(page.url, anchor.get("href", ""))
        if not is_useful_url(href):
            continue
        if text.lower() in seen_names:
            continue
        parent_text = clean_text(anchor.parent.get_text(" ", strip=True)) if anchor.parent else text
        score = _candidate_anchor_score(text, href, parent_text)
        if score < 1.0:
            continue
        seen_names.add(text.lower())
        candidates.append((score, text, href, parent_text))

    candidates.sort(key=lambda item: item[0], reverse=True)
    drafts: list[EntityDraft] = []
    page_domain = extract_domain(page.url)

    for _, name, href, parent_text in candidates[:12]:
        cells: dict[str, CellDraft] = {}
        _add_cell(cells, plan, page, "name", name, evidence_hint=name, confidence=0.78)
        href_domain = extract_domain(href)
        if href_domain and href_domain != page_domain:
            for website_col in ("website", "url", "homepage", "site", "website_or_profile", "website_or_repo"):
                _add_cell(cells, plan, page, website_col, href, evidence_hint=href, confidence=0.72)

        address_match = _ADDRESS_RE.search(parent_text)
        if address_match:
            address = clean_text(address_match.group(0))
            for location_col in ("location", "address", "headquarters"):
                _add_cell(cells, plan, page, location_col, address, evidence_hint=address, confidence=0.68)

        draft = _entity_draft(name, cells, page)
        if draft:
            drafts.append(draft)

    return drafts


def extract_deterministic_entities(
    query: str,
    plan: PlannerOutput,
    page: ScrapedPage,
    *,
    mode: str = "fill",
) -> list[EntityDraft]:
    """Extract entities without LLM calls when the page structure is clear."""
    regime = page.evidence_regime

    if regime == "software_repo_or_docs":
        return _extract_software_repo_or_docs(page, plan)

    if regime == "local_business_listing":
        local = _extract_local_business(page, plan)
        if local:
            return local

    if regime == "official_site":
        official = _extract_official_page(page, plan)
        if official:
            return official

    if mode == "discovery" and regime in {"directory_listing", "local_business_listing"}:
        return _extract_directory_candidates(page, plan)

    return []
