"""
Evaluate how well each EntityRow satisfies parsed RequirementSpecs.

Produces RequirementMatch objects with three-state evaluation:
  satisfied      — row clearly meets the requirement
  not_satisfied  — row clearly fails the requirement
  unknown        — field is absent; we cannot judge (NOT treated as failure)

Entry points:
  evaluate_requirement(spec, row)          → RequirementMatch
  build_requirement_summary(specs, row)    → RowRequirementsSummary
  attach_requirement_summaries(rows, specs) → list[EntityRow]  (mutates in-place)
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.models.schema import (
    EntityRow,
    RequirementEvidence,
    RequirementMatch,
    RequirementSpec,
    RowRequirementsSummary,
)

log = get_logger(__name__)

# ── Money parsing ──────────────────────────────────────────────────────────────

_MONEY_SUFFIXES = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "billion": 1_000_000_000,
}
_MONEY_RE = re.compile(r"\$?(\d[\d,]*(?:\.\d+)?)\s*([kmb]|thousand|million|billion)?", re.IGNORECASE)


def _parse_money(text: str) -> float | None:
    m = _MONEY_RE.search(text)
    if not m:
        return None
    base = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    return base * _MONEY_SUFFIXES.get(suffix, 1)


def _parse_number(text: str) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


# ── Location normalisation ─────────────────────────────────────────────────────

_US_STATE_ABBREVS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

_EU_COUNTRIES = {
    "austria","belgium","bulgaria","croatia","cyprus","czechia","czech republic",
    "denmark","estonia","finland","france","germany","greece","hungary","ireland",
    "italy","latvia","lithuania","luxembourg","malta","netherlands","poland",
    "portugal","romania","slovakia","slovenia","spain","sweden",
}

_US_STATE_NAMES = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    "washington dc","district of columbia",
}


def _normalize_location(raw: str) -> str:
    """Normalize location tokens to canonical lowercase slugs for comparison."""
    s = raw.strip().lower()
    if s in ("us", "usa", "united states", "united states of america", "america"):
        return "us"
    if s in ("uk", "gb", "united kingdom", "great britain", "britain", "england"):
        return "uk"
    if s in ("eu", "europe", "european union"):
        return "eu"
    if s in ("ca", "canada"):
        return "canada"
    if s in ("au", "australia"):
        return "australia"
    if s.upper() in _US_STATE_ABBREVS:
        return f"us-{s}"  # state abbrev → "us-ny"
    if s in _US_STATE_NAMES:
        return f"us-state"
    if s in _EU_COUNTRIES:
        return "eu"
    return s


def _location_matches(cell_value: str, target: str) -> bool:
    """
    Check whether *cell_value* (from a row cell) satisfies a location requirement
    with *target* (already normalised). Uses substring and normalisation.
    """
    candidates = {_normalize_location(cell_value)}
    for part in re.split(r"[,/|()\-]", cell_value):
        cleaned = part.strip()
        if cleaned:
            candidates.add(_normalize_location(cleaned))

    if target in candidates:
        return True
    if target == "us" and any(candidate.startswith("us-") or candidate == "us-state" for candidate in candidates):
        return True
    if target == "eu" and "eu" in candidates:
        return True
    return target in cell_value.lower()


# ── Numeric comparison ─────────────────────────────────────────────────────────

def _numeric_compare(cell_text: str, operator: str, threshold_text: str, money: bool) -> bool | None:
    """Return True/False/None (None = unparseable → unknown)."""
    parse = _parse_money if money else _parse_number
    cell_val = parse(cell_text)
    threshold = parse(threshold_text)
    if cell_val is None or threshold is None:
        return None
    if operator == "greater_than":
        return cell_val > threshold
    if operator == "less_than":
        return cell_val < threshold
    if operator == "at_least":
        return cell_val >= threshold
    if operator == "equals":
        return cell_val == threshold
    return None


# ── Field column lookup ────────────────────────────────────────────────────────

_FIELD_ALIASES: dict[str, list[str]] = {
    "location":  ["location", "address", "headquarters", "hq", "country", "city", "region"],
    "funding":   ["funding", "raised", "funding_raised", "total_raised", "valuation", "capital"],
    "stage":     ["stage", "stage_or_status", "status", "funding_stage", "company_stage"],
    "license":   ["license", "licence", "open_source", "license_type"],
    "founded":   ["founded", "founded_year", "year_founded", "established"],
    "employees": ["employees", "team_size", "headcount", "size"],
    "category":  ["category", "industry", "sector", "type", "vertical"],
    "topic":     ["category", "industry", "sector", "type", "vertical", "description", "about"],
}


def _candidate_cells(mapped_columns: list[str], row: EntityRow):
    """Yield (column_name, Cell) pairs for all columns that have data."""
    seen: set[str] = set()
    for col in mapped_columns:
        if col in seen:
            continue
        seen.add(col)
        cell = row.cells.get(col)
        if cell and cell.value:
            yield col, cell


def _normalize_text(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def _match_confidence(cell_confidence: float, source_kind: str) -> float:
    multiplier = {
        "value": 1.0,
        "evidence_snippet": 0.9,
        "source_title": 0.8,
    }.get(source_kind, 1.0)
    return round(max(0.0, min(1.0, cell_confidence * multiplier)), 3)


def _match_evidence(cell) -> RequirementEvidence | None:
    if not cell:
        return None
    return RequirementEvidence(
        source_url=cell.source_url,
        source_title=cell.source_title,
        evidence_snippet=cell.evidence_snippet,
    )


def _grounded_texts(cell, *, include_title: bool = True) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    if cell.value:
        texts.append(("value", cell.value))
    if cell.evidence_snippet and cell.evidence_snippet != cell.value:
        texts.append(("evidence_snippet", cell.evidence_snippet))
    if include_title and cell.source_title and cell.source_title not in {cell.value, cell.evidence_snippet}:
        texts.append(("source_title", cell.source_title))
    return texts


def _make_match(
    spec: RequirementSpec,
    *,
    status: str,
    confidence: float,
    matched_value: str | None = None,
    matched_column: str | None = None,
    reason: str | None = None,
    cell=None,
) -> RequirementMatch:
    return RequirementMatch(
        requirement_id=spec.id,
        label=spec.label,
        status=status,
        confidence=confidence,
        matched_value=matched_value,
        matched_column=matched_column,
        reason=reason,
        evidence=_match_evidence(cell),
        is_hard=spec.is_hard,
    )


# ── Core evaluator ─────────────────────────────────────────────────────────────


def evaluate_requirement(spec: RequirementSpec, row: EntityRow) -> RequirementMatch:
    """
    Evaluate a single RequirementSpec against an EntityRow.

    Returns a RequirementMatch with status = satisfied | not_satisfied | unknown.
    Unknown means the relevant field(s) are absent — we cannot confirm or deny.
    """
    # Determine which columns to check
    mapped = spec.mapped_columns
    if not mapped:
        # Fall back to field aliases keyed by the id prefix
        prefix = spec.id.split("_")[0]
        mapped = _FIELD_ALIASES.get(prefix, [])

    # Gather candidate cells
    candidate_cells = list(_candidate_cells(mapped, row))

    # No relevant field present → unknown
    if not candidate_cells:
        return _make_match(
            spec,
            status="unknown",
            confidence=0.0,
            reason="No relevant field found in row",
        )

    operator = spec.operator
    target = spec.target_value or ""

    # ── exists ────────────────────────────────────────────────────────────────
    if operator == "exists":
        col_name, cell = candidate_cells[0]
        return _make_match(
            spec,
            status="satisfied",
            confidence=cell.confidence,
            matched_value=cell.value,
            matched_column=col_name,
            reason=f"Field '{col_name}' is present",
            cell=cell,
        )

    # ── location ──────────────────────────────────────────────────────────────
    if spec.kind == "location":
        norm_target = _normalize_location(target)
        for col_name, cell in candidate_cells:
            for source_kind, text in _grounded_texts(cell):
                if not _location_matches(text, norm_target):
                    continue
                return _make_match(
                    spec,
                    status="satisfied",
                    confidence=_match_confidence(cell.confidence, source_kind),
                    matched_value=text,
                    matched_column=col_name,
                    reason=f"Location evidence matches '{spec.target_value_raw or target}'",
                    cell=cell,
                )
        # Fields present but no match
        first_col, first_cell = candidate_cells[0]
        return _make_match(
            spec,
            status="not_satisfied",
            confidence=first_cell.confidence,
            matched_value=first_cell.value,
            matched_column=first_col,
            reason=f"'{first_cell.value}' does not match location '{spec.target_value_raw or target}'",
            cell=first_cell,
        )

    # ── numeric ───────────────────────────────────────────────────────────────
    if spec.kind == "numeric":
        hint_text = f"{spec.id} {spec.label} {spec.source_phrase} {spec.target_value or ''}".lower()
        is_money = any(token in hint_text for token in ("fund", "rais", "valuation", "capital", "$"))
        satisfied_match: RequirementMatch | None = None
        failed_match: RequirementMatch | None = None
        for col_name, cell in candidate_cells:
            for source_kind, text in _grounded_texts(cell, include_title=False):
                result = _numeric_compare(text, operator, target, money=is_money)
                if result is None:
                    continue
                status = "satisfied" if result else "not_satisfied"
                reason = (
                    f"'{text}' {'satisfies' if result else 'does not satisfy'} "
                    f"requirement {operator.replace('_', ' ')} {spec.target_value_raw or target}"
                )
                match = _make_match(
                    spec,
                    status=status,
                    confidence=_match_confidence(cell.confidence, source_kind),
                    matched_value=text,
                    matched_column=col_name,
                    reason=reason,
                    cell=cell,
                )
                if result:
                    if satisfied_match is None or match.confidence > satisfied_match.confidence:
                        satisfied_match = match
                elif failed_match is None or match.confidence > failed_match.confidence:
                    failed_match = match
        if satisfied_match:
            return satisfied_match
        if failed_match:
            return failed_match
        # No cell yielded a parseable number
        first_col, first_cell = candidate_cells[0]
        return _make_match(
            spec,
            status="unknown",
            confidence=0.0,
            matched_value=first_cell.value,
            matched_column=first_col,
            reason=f"Could not parse numeric value from '{first_cell.value}'",
            cell=first_cell,
        )

    # ── categorical / semantic / default ─────────────────────────────────────
    target_norm = _normalize_text(target)
    for col_name, cell in candidate_cells:
        for source_kind, text in _grounded_texts(cell):
            text_norm = _normalize_text(text)
            if operator == "equals":
                matched = text_norm == target_norm or target_norm in text_norm
            elif operator in ("contains", "matches_topic"):
                matched = target_norm in text_norm
            else:
                matched = target_norm in text_norm

            if matched:
                return _make_match(
                    spec,
                    status="satisfied",
                    confidence=_match_confidence(cell.confidence, source_kind),
                    matched_value=text,
                    matched_column=col_name,
                    reason=f"Matched '{spec.target_value_raw or target}' in {col_name.replace('_', ' ')}",
                    cell=cell,
                )

    # Fields present but no string match
    first_col, first_cell = candidate_cells[0]
    return _make_match(
        spec,
        status="not_satisfied",
        confidence=first_cell.confidence,
        matched_value=first_cell.value,
        matched_column=first_col,
        reason=f"'{first_cell.value}' does not match '{spec.target_value_raw or target}'",
        cell=first_cell,
    )


# ── Summary builder ────────────────────────────────────────────────────────────


def build_requirement_summary(
    specs: list[RequirementSpec],
    row: EntityRow,
) -> RowRequirementsSummary:
    """Evaluate all specs against a row and return a RowRequirementsSummary."""
    if not specs:
        return RowRequirementsSummary()

    matches: list[RequirementMatch] = []
    sat = 0
    not_sat = 0
    unk = 0
    hard_sat = 0

    for spec in specs:
        match = evaluate_requirement(spec, row)
        per_requirement = 1.0 / len(specs)
        if match.status == "satisfied":
            match.score_contribution = round(per_requirement, 3)
        elif match.status == "unknown":
            match.score_contribution = round(per_requirement * 0.5, 3)
        else:
            match.score_contribution = 0.0
        matches.append(match)
        if match.status == "satisfied":
            sat += 1
            if spec.is_hard:
                hard_sat += 1
        elif match.status == "not_satisfied":
            not_sat += 1
        else:
            unk += 1

    satisfaction_ratio = (sat / len(specs)) if specs else 0.0

    return RowRequirementsSummary(
        requirements_total_count=len(specs),
        requirements_satisfied_count=sat,
        requirements_not_satisfied_count=not_sat,
        requirements_unknown_count=unk,
        satisfaction_ratio=round(satisfaction_ratio, 3),
        hard_requirements_satisfied_count=hard_sat,
        matches=matches,
    )


# ── Batch attachment ───────────────────────────────────────────────────────────


def attach_requirement_summaries(
    rows: list[EntityRow],
    specs: list[RequirementSpec],
) -> list[EntityRow]:
    """
    Evaluate all specs against each row and attach a RowRequirementsSummary.
    Mutates rows in-place. Returns the same list for chaining.
    """
    if not specs:
        return rows

    for row in rows:
        row.requirement_summary = build_requirement_summary(specs, row)

    total_matches = sum(len(row.requirement_summary.matches) for row in rows)
    sat = sum(row.requirement_summary.requirements_satisfied_count for row in rows)
    not_sat = sum(row.requirement_summary.requirements_not_satisfied_count for row in rows)
    unk = sum(row.requirement_summary.requirements_unknown_count for row in rows)
    log.info(
        "Requirement summaries attached: %d rows, %d specs, matches=%d (sat=%d not_sat=%d unknown=%d)",
        len(rows),
        len(specs),
        total_matches,
        sat,
        not_sat,
        unk,
    )
    return rows
