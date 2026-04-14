"""
Parse structured requirements from a free-text search query.

Two entry points:
  parse_requirements_deterministic(query) → fast regex path, no LLM
  parse_requirements(query)               → LLM first, deterministic fallback

RequirementSpec captures constraints that must be satisfied by result rows, such as:
  - location constraints: "in the US", "based in Europe"
  - funding constraints: "funding > $10M", "raised more than 50M"
  - stage constraints: "startups", "Series B", "publicly traded"
  - license constraints: "open source", "MIT license"
  - founding year: "founded after 2010"
  - semantic/categorical: "search engine", "nonprofit", "SaaS"
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.models.schema import PlannerOutput, RequirementSpec

log = get_logger(__name__)

# ── Column alias map ──────────────────────────────────────────────────────────
# Maps requirement field names → candidate schema column names to check.

_FIELD_COLUMNS: dict[str, list[str]] = {
    "location":  ["location", "address", "headquarters", "hq", "country", "city", "region"],
    "funding":   ["funding", "raised", "funding_raised", "total_raised", "valuation", "capital"],
    "stage":     ["stage", "stage_or_status", "status", "funding_stage", "company_stage"],
    "license":   ["license", "licence", "open_source", "license_type"],
    "founded":   ["founded", "founded_year", "year_founded", "established"],
    "employees": ["employees", "team_size", "headcount", "size"],
    "category":  ["category", "industry", "sector", "type", "vertical"],
    "topic":     ["category", "industry", "sector", "type", "vertical", "description", "about"],
}

_FAMILY_COLUMN_BINDINGS: dict[str, dict[str, list[str]]] = {
    "organization_company": {
        "location": ["headquarters"],
        "topic": ["focus_area", "product_or_service"],
        "stage": ["stage_or_status"],
        "funding": ["funding"],
        "founded": ["founded"],
        "employees": ["employees"],
        "license": ["license"],
    },
    "place_venue": {
        "location": ["location", "address"],
        "topic": ["category", "offering"],
        "price": ["price_or_availability"],
        "founded": ["founded"],
    },
    "software_project": {
        "topic": ["primary_use_case"],
        "license": ["license"],
        "maintainer": ["maintainer_or_org"],
        "language": ["language_or_stack"],
        "website": ["website_or_repo"],
    },
    "product_offering": {
        "topic": ["category", "key_feature"],
        "price": ["price_or_availability"],
        "maker": ["maker_or_brand"],
    },
    "person_group": {
        "location": ["location"],
        "topic": ["role_or_title", "notable_work"],
        "affiliation": ["affiliation"],
        "website": ["website_or_profile"],
    },
    "generic_entity_list": {
        "location": ["location"],
        "topic": ["description", "category"],
        "website": ["website"],
    },
}

# ── Location regex ─────────────────────────────────────────────────────────────
# Explicit case variants for prepositions; capture group stays case-sensitive to
# prevent "in the US with funding" from absorbing lowercase words.
_LOCATION_RE = re.compile(
    r"\b(?:[Ii]n|[Ff]rom|[Bb]ased\s+in|[Ll]ocated\s+in|[Hh]eadquartered\s+in)\s+"
    r"(?:[Tt]he\s+)?([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){0,3}|US|UK|EU|USA|NYC)\b"
)

# US state abbreviations (2-letter uppercase only — prevents matching "IN" as a state mid-sentence)
_US_STATE_ABBREVS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

def _normalize_location(raw: str) -> str:
    """Normalize location tokens to canonical lowercase slugs."""
    s = raw.strip()
    upper = s.upper()
    if upper in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", "AMERICA"):
        return "us"
    if upper in ("UK", "GB", "UNITED KINGDOM", "GREAT BRITAIN", "BRITAIN"):
        return "uk"
    if upper in ("EU", "EUROPE", "EUROPEAN UNION"):
        return "eu"
    if upper in ("CA", "CANADA"):
        return "canada"
    if upper in ("AU", "AUSTRALIA"):
        return "australia"
    # State abbreviations → normalize to lowercase but keep as-is for matching
    if upper in _US_STATE_ABBREVS:
        return upper.lower()
    return s.lower()


# ── Funding ───────────────────────────────────────────────────────────────────

_MONEY_VALUE_RE = r"\d+(?:\.\d+)?(?:\s*(?:k|m|b|thousand|million|billion))?"

_FUNDING_GT_RE = re.compile(
    rf"\b(?:funding|raised|valuation)\s*(>|≥|over|more\s+than|greater\s+than|at\s+least)\s*"
    rf"\$?({_MONEY_VALUE_RE})\b",
    re.IGNORECASE,
)
_FUNDING_LT_RE = re.compile(
    rf"\b(?:funding|raised|valuation)\s*(<|≤|under|less\s+than|below)\s*"
    rf"\$?({_MONEY_VALUE_RE})\b",
    re.IGNORECASE,
)

def _normalize_money(raw: str) -> str:
    s = raw.strip().lower().replace(",", "")
    s = re.sub(r"\s+", "", s)
    for word, suffix in (("thousand", "K"), ("million", "M"), ("billion", "B")):
        if s.endswith(word):
            return f"{s[:-len(word)]}{suffix}".upper()
    return s.upper() if s and s[-1].isalpha() else s


# ── Stage ─────────────────────────────────────────────────────────────────────

_STAGE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bearly[- ]stage\b", re.IGNORECASE), "early-stage"),
    (re.compile(r"\bpre[- ]seed\b",    re.IGNORECASE), "pre-seed"),
    (re.compile(r"\bseed[- ]stage\b",  re.IGNORECASE), "seed"),
    (re.compile(r"\bseries\s+([a-d])\b", re.IGNORECASE), "__series__"),
    (re.compile(r"\bstartups?\b",      re.IGNORECASE), "startup"),
    (re.compile(r"\bpublic(?:ly\s+(?:listed|traded))?\b", re.IGNORECASE), "public"),
    (re.compile(r"\bprivate(?:ly\s+held)?\b", re.IGNORECASE), "private"),
    (re.compile(r"\bbootstrapped\b",   re.IGNORECASE), "bootstrapped"),
]

# ── License ───────────────────────────────────────────────────────────────────

_OPEN_SOURCE_RE = re.compile(r"\bopen[- ]source\b", re.IGNORECASE)
_LICENSE_RE = re.compile(r"\b(mit|apache|gpl|bsd|mpl|lgpl)\s+licen[cs]e?\b", re.IGNORECASE)

# ── Founding year ─────────────────────────────────────────────────────────────

_FOUNDED_EXACT_RE  = re.compile(r"\bfounded\s+(?:in\s+)?(\d{4})\b", re.IGNORECASE)
_FOUNDED_AFTER_RE  = re.compile(r"\bfounded\s+after\s+(\d{4})\b",   re.IGNORECASE)
_FOUNDED_BEFORE_RE = re.compile(r"\bfounded\s+before\s+(\d{4})\b",  re.IGNORECASE)

# ── Employee / size ───────────────────────────────────────────────────────────

_SIZE_GT_RE = re.compile(
    r"\b(?:more\s+than|over|at\s+least)\s+(\d+)\s+(?:employees|people|staff)\b",
    re.IGNORECASE,
)
_SIZE_LT_RE = re.compile(
    r"\b(?:fewer\s+than|under|less\s+than)\s+(\d+)\s+(?:employees|people|staff)\b",
    re.IGNORECASE,
)

# ── Categorical / semantic ─────────────────────────────────────────────────────
# Entity-type trigger words — when a compound modifier precedes these, extract
# the compound as a "topic" or "category" requirement.
_ENTITY_TYPE_WORDS = {
    "startup", "startups", "company", "companies", "firm", "firms",
    "tool", "tools", "platform", "platforms", "product", "products",
    "service", "services", "app", "apps", "software", "framework",
    "library", "database", "restaurant", "restaurants", "cafe", "cafes",
    "agency", "agencies", "studio", "studios",
}
# Qualifier words to strip from the compound modifier
_QUALIFIER_WORDS = {
    "top", "best", "leading", "popular", "major", "top-rated",
    "well-known", "famous", "notable", "prominent", "successful",
    "innovative", "emerging",
}

def _extract_semantic_requirements(query: str) -> list[RequirementSpec]:
    """
    Detect compound modifiers before entity-type words.

    Example: "search engine startups" → topic requirement "search engine"
             "nonprofit healthcare companies" → topic requirements "nonprofit" + "healthcare"
    """
    specs: list[RequirementSpec] = []
    tokens = query.split()
    for i, token in enumerate(tokens):
        if token.lower() in _ENTITY_TYPE_WORDS and i >= 1:
            # Collect preceding non-qualifier words as the topic
            compound_parts: list[str] = []
            j = i - 1
            while j >= 0 and tokens[j].lower() not in _QUALIFIER_WORDS:
                word = tokens[j].lower().rstrip(",;")
                # Stop at prepositions, articles, or conjunctions
                if word in {"in", "from", "at", "with", "for", "and", "or", "the", "a", "an"}:
                    break
                compound_parts.insert(0, tokens[j])
                j -= 1

            if compound_parts:
                raw_phrase = " ".join(compound_parts)
                topic = raw_phrase.lower()
                source_tokens = compound_parts + [tokens[i]]
                source_phrase = " ".join(source_tokens)
                specs.append(RequirementSpec(
                    id=f"topic_{len(specs)}",
                    label=f"Topic: {topic}",
                    kind="semantic",
                    operator="matches_topic",
                    target_value=topic,
                    target_value_raw=raw_phrase,
                    source_phrase=source_phrase,
                    priority="medium",
                    is_hard=False,
                    mapped_columns=_FIELD_COLUMNS["topic"],
                ))
    return specs


def _requirement_binding_key(spec: RequirementSpec) -> str:
    source = f"{spec.source_phrase} {spec.label} {spec.target_value or ''}".lower()
    target = (spec.target_value or "").lower()

    if spec.kind == "location":
        return "location"
    if spec.kind == "semantic":
        return "topic"
    if spec.kind == "numeric":
        if any(token in source for token in ("funding", "raised", "valuation", "capital")):
            return "funding"
        if any(token in source for token in ("founded", "established", "year founded")):
            return "founded"
        if any(token in source for token in ("employee", "employees", "people", "staff", "headcount", "team size")):
            return "employees"
        if any(token in source for token in ("price", "$", "cost", "under", "over")):
            return "price"
        return "numeric"
    if spec.kind == "categorical":
        if "license" in source or target in {"open-source", "mit", "apache", "gpl", "bsd", "mpl", "lgpl"}:
            return "license"
        if target in {"startup", "early-stage", "pre-seed", "seed", "public", "private", "bootstrapped"} or target.startswith("series "):
            return "stage"
        return "category"
    return "topic"


def _preferred_columns_for_requirement(spec: RequirementSpec, query_family: str) -> list[str]:
    key = _requirement_binding_key(spec)
    preferred = list(_FAMILY_COLUMN_BINDINGS.get(query_family, {}).get(key, []))
    for col in spec.mapped_columns:
        if col not in preferred:
            preferred.append(col)
    return preferred


def _augmentable_columns_for_requirement(spec: RequirementSpec, query_family: str) -> list[str]:
    key = _requirement_binding_key(spec)
    return list(_FAMILY_COLUMN_BINDINGS.get(query_family, {}).get(key, []))


def augment_plan_with_requirements(
    plan: PlannerOutput,
    specs: list[RequirementSpec],
    *,
    max_columns: int = 8,
) -> PlannerOutput:
    """Expand the schema conservatively so evaluable requirements have a home."""
    columns = list(plan.columns)
    changed = False

    for spec in specs:
        augmentable = _augmentable_columns_for_requirement(spec, plan.query_family)
        if not augmentable or any(col in columns for col in augmentable):
            continue
        for col in augmentable:
            if col in columns:
                continue
            if len(columns) >= max_columns:
                break
            columns.append(col)
            changed = True

    if not changed:
        return plan
    return plan.model_copy(update={"columns": columns})


def bind_requirements_to_plan(
    specs: list[RequirementSpec],
    plan: PlannerOutput,
) -> list[RequirementSpec]:
    """Map parsed requirements onto the schema columns actually used for extraction."""
    bound: list[RequirementSpec] = []
    for spec in specs:
        mapped_columns = [
            col
            for col in _preferred_columns_for_requirement(spec, plan.query_family)
            if col in plan.columns
        ]
        bound.append(spec.model_copy(update={"mapped_columns": mapped_columns}))
    return bound


def prepare_requirements(
    query: str,
    *,
    normalized_query: str | None = None,
    plan: PlannerOutput | None = None,
) -> tuple[list[RequirementSpec], PlannerOutput | None]:
    """
    Parse requirements from the original query text, then align them to the plan.

    The original query preserves comparison operators such as `>` that the
    normalizer intentionally strips before retrieval planning.
    """
    specs = parse_requirements_deterministic(query)
    if not specs and normalized_query and normalized_query != query:
        specs = parse_requirements_deterministic(normalized_query)

    if plan is None or not specs:
        return specs, plan

    augmented_plan = augment_plan_with_requirements(plan, specs)
    return bind_requirements_to_plan(specs, augmented_plan), augmented_plan


def parse_requirements_deterministic(query: str) -> list[RequirementSpec]:
    """Extract structured requirements using regex — no LLM, no latency."""
    specs: list[RequirementSpec] = []
    counters: dict[str, int] = {}

    def _next_id(kind: str) -> str:
        counters[kind] = counters.get(kind, 0)
        idx = counters[kind]
        counters[kind] += 1
        return f"{kind}_{idx}"

    # ── Location ──────────────────────────────────────────────────────────────
    for m in _LOCATION_RE.finditer(query):
        raw = m.group(1).strip()
        norm = _normalize_location(raw)
        specs.append(RequirementSpec(
            id=_next_id("loc"),
            label=f"Location: {raw}",
            kind="location",
            operator="contains",
            target_value=norm,
            target_value_raw=raw,
            source_phrase=m.group(0).strip(),
            priority="high",
            is_hard=True,
            mapped_columns=_FIELD_COLUMNS["location"],
        ))

    # ── Funding ───────────────────────────────────────────────────────────────
    for m in _FUNDING_GT_RE.finditer(query):
        raw_operator = m.group(1).strip().lower()
        raw = m.group(2)
        norm = _normalize_money(raw)
        operator = "at_least" if raw_operator in {"≥", "at least"} else "greater_than"
        label_operator = "≥" if operator == "at_least" else ">"
        specs.append(RequirementSpec(
            id=_next_id("fund"),
            label=f"Funding {label_operator} {norm}",
            kind="numeric",
            operator=operator,
            target_value=norm,
            target_value_raw=raw.strip(),
            source_phrase=m.group(0).strip(),
            priority="high",
            is_hard=True,
            mapped_columns=_FIELD_COLUMNS["funding"],
        ))
    for m in _FUNDING_LT_RE.finditer(query):
        raw = m.group(2)
        norm = _normalize_money(raw)
        specs.append(RequirementSpec(
            id=_next_id("fund"),
            label=f"Funding < {norm}",
            kind="numeric",
            operator="less_than",
            target_value=norm,
            target_value_raw=raw.strip(),
            source_phrase=m.group(0).strip(),
            priority="high",
            is_hard=True,
            mapped_columns=_FIELD_COLUMNS["funding"],
        ))

    # ── Stage ─────────────────────────────────────────────────────────────────
    for pattern, stage_value in _STAGE_RULES:
        m = pattern.search(query)
        if m:
            if stage_value == "__series__":
                actual = f"Series {m.group(1).upper()}"
            else:
                actual = stage_value
            specs.append(RequirementSpec(
                id=_next_id("stage"),
                label=f"Stage: {actual}",
                kind="categorical",
                operator="contains",
                target_value=actual.lower(),
                target_value_raw=m.group(0).strip(),
                source_phrase=m.group(0).strip(),
                priority="medium",
                is_hard=False,
                mapped_columns=_FIELD_COLUMNS["stage"],
            ))
            break  # only first stage match

    # ── License ───────────────────────────────────────────────────────────────
    m = _OPEN_SOURCE_RE.search(query)
    if m:
        specs.append(RequirementSpec(
            id=_next_id("lic"),
            label="License: open-source",
            kind="categorical",
            operator="exists",
            target_value="open-source",
            target_value_raw=m.group(0).strip(),
            source_phrase=m.group(0).strip(),
            priority="medium",
            is_hard=False,
            mapped_columns=_FIELD_COLUMNS["license"],
        ))
    else:
        m = _LICENSE_RE.search(query)
        if m:
            specs.append(RequirementSpec(
                id=_next_id("lic"),
                label=f"License: {m.group(1).upper()}",
                kind="categorical",
                operator="contains",
                target_value=m.group(1).upper(),
                target_value_raw=m.group(0).strip(),
                source_phrase=m.group(0).strip(),
                priority="medium",
                is_hard=False,
                mapped_columns=_FIELD_COLUMNS["license"],
            ))

    # ── Founding year ─────────────────────────────────────────────────────────
    m = _FOUNDED_AFTER_RE.search(query)
    if m:
        specs.append(RequirementSpec(
            id=_next_id("founded"),
            label=f"Founded after {m.group(1)}",
            kind="numeric",
            operator="greater_than",
            target_value=m.group(1),
            target_value_raw=m.group(0).strip(),
            source_phrase=m.group(0).strip(),
            priority="medium",
            is_hard=False,
            mapped_columns=_FIELD_COLUMNS["founded"],
        ))
    else:
        m = _FOUNDED_BEFORE_RE.search(query)
        if m:
            specs.append(RequirementSpec(
                id=_next_id("founded"),
                label=f"Founded before {m.group(1)}",
                kind="numeric",
                operator="less_than",
                target_value=m.group(1),
                target_value_raw=m.group(0).strip(),
                source_phrase=m.group(0).strip(),
                priority="medium",
                is_hard=False,
                mapped_columns=_FIELD_COLUMNS["founded"],
            ))
        else:
            m = _FOUNDED_EXACT_RE.search(query)
            if m:
                specs.append(RequirementSpec(
                    id=_next_id("founded"),
                    label=f"Founded in {m.group(1)}",
                    kind="numeric",
                    operator="equals",
                    target_value=m.group(1),
                    target_value_raw=m.group(0).strip(),
                    source_phrase=m.group(0).strip(),
                    priority="medium",
                    is_hard=False,
                    mapped_columns=_FIELD_COLUMNS["founded"],
                ))

    # ── Employees / size ──────────────────────────────────────────────────────
    m = _SIZE_GT_RE.search(query)
    if m:
        specs.append(RequirementSpec(
            id=_next_id("emp"),
            label=f"Employees > {m.group(1)}",
            kind="numeric",
            operator="greater_than",
            target_value=m.group(1),
            target_value_raw=m.group(0).strip(),
            source_phrase=m.group(0).strip(),
            priority="medium",
            is_hard=False,
            mapped_columns=_FIELD_COLUMNS["employees"],
        ))
    else:
        m = _SIZE_LT_RE.search(query)
        if m:
            specs.append(RequirementSpec(
                id=_next_id("emp"),
                label=f"Employees < {m.group(1)}",
                kind="numeric",
                operator="less_than",
                target_value=m.group(1),
                target_value_raw=m.group(0).strip(),
                source_phrase=m.group(0).strip(),
                priority="medium",
                is_hard=False,
                mapped_columns=_FIELD_COLUMNS["employees"],
            ))

    # ── Semantic / categorical compound modifiers ─────────────────────────────
    semantic_specs = _extract_semantic_requirements(query)
    # Avoid duplicating topics already captured as stage/license
    existing_topics = {s.target_value for s in specs}
    for s in semantic_specs:
        if s.target_value not in existing_topics:
            s.id = _next_id("topic")
            specs.append(s)
            existing_topics.add(s.target_value)

    log.debug("Deterministic requirements for %r: %d parsed", query, len(specs))
    return specs


# ── LLM-based parser ──────────────────────────────────────────────────────────

_LLM_SYSTEM = """\
You are a query requirement extractor.
Given a search query, identify any explicit hard constraints the user expressed.
Return a JSON object with a single key "requirements" whose value is an array.
Each array element must have:
  id            (string): short slug like "loc_0", "fund_1"
  label         (string): human-readable label, e.g. "Location: US"
  kind          (string): one of: categorical, location, numeric, semantic
  operator      (string): one of: equals, contains, greater_than, less_than, at_least, exists, matches_topic
  target_value  (string): normalized constraint value, e.g. "us", "10M", "startup"
  target_value_raw (string): the raw value from the query
  source_phrase (string): the exact substring of the query this came from
  priority      (string): "high" or "medium"
  is_hard       (boolean): true if failing this should significantly penalize ranking
  mapped_columns (array of strings): schema columns that should hold this value

Rules:
- Only extract HARD or clearly expressed constraints.
- Do NOT invent requirements not in the query.
- Do NOT extract the main topic (e.g. "restaurants" is not a requirement).
- For vague descriptors like "top", "best", "leading" — do NOT emit a requirement.
- If there are no constraints, return {"requirements": []}.
"""

_LLM_USER_TEMPLATE = 'Query: "{query}"'


async def parse_requirements(query: str) -> list[RequirementSpec]:
    """Parse requirements with LLM, falling back to deterministic on any failure."""
    try:
        return await _parse_with_llm(query)
    except Exception as exc:
        log.warning("LLM requirement parsing failed (%s), using deterministic fallback", exc)
        return parse_requirements_deterministic(query)


async def _parse_with_llm(query: str) -> list[RequirementSpec]:
    from app.services.llm import chat_json  # noqa: PLC0415

    raw = await chat_json(
        _LLM_SYSTEM,
        _LLM_USER_TEMPLATE.format(query=query),
        temperature=0.1,
        max_tokens=512,
    )

    items = raw.get("requirements", [])
    if not isinstance(items, list):
        raise ValueError(f"Expected list under 'requirements', got {type(items)}")

    specs: list[RequirementSpec] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            specs.append(RequirementSpec(
                id=str(item.get("id") or f"llm_{len(specs)}"),
                label=str(item.get("label", "")),
                kind=str(item.get("kind", "categorical")),
                operator=str(item.get("operator", "contains")),
                target_value=str(item.get("target_value", "")) or None,
                target_value_raw=str(item.get("target_value_raw", "")) or None,
                source_phrase=str(item.get("source_phrase", "")),
                priority=str(item.get("priority", "medium")),
                is_hard=bool(item.get("is_hard", False)),
                mapped_columns=list(item.get("mapped_columns", [])),
                notes=str(item.get("notes", "")) or None,
            ))
        except Exception:
            continue

    specs = [s for s in specs if s.target_value or s.operator == "exists"]
    log.debug("LLM requirements for %r: %d parsed", query, len(specs))
    return specs
