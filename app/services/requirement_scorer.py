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
from app.models.schema import EntityRow, RequirementMatch, RequirementSpec, RowRequirementsSummary

log = get_logger(__name__)

# ── Money parsing ──────────────────────────────────────────────────────────────

_MONEY_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_MONEY_RE = re.compile(r"\$?(\d+(?:\.\d+)?)\s*([kmb])?", re.IGNORECASE)


def _parse_money(text: str) -> float | None:
    m = _MONEY_RE.search(text)
    if not m:
        return None
    base = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    return base * _MONEY_SUFFIXES.get(suffix, 1)


def _parse_number(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


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
    cell_norm = _normalize_location(cell_value)
    # Direct normalised match
    if cell_norm == target:
        return True
    # If target is "us", also accept any US state
    if target == "us" and (cell_norm.startswith("us-") or cell_norm == "us-state"):
        return True
    # If target is "eu", accept any EU country
    if target == "eu" and cell_norm == "eu":
        return True
    # Raw substring match (case-insensitive) as fallback
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
    if operator in ("equals", "at_least"):
        return cell_val >= threshold
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
        return RequirementMatch(
            requirement_id=spec.id,
            label=spec.label,
            status="unknown",
            confidence=0.0,
            reason="No relevant field found in row",
            is_hard=spec.is_hard,
        )

    operator = spec.operator
    target = spec.target_value or ""

    # ── exists ────────────────────────────────────────────────────────────────
    if operator == "exists":
        col_name, cell = candidate_cells[0]
        return RequirementMatch(
            requirement_id=spec.id,
            label=spec.label,
            status="satisfied",
            confidence=cell.confidence,
            matched_value=cell.value,
            matched_column=col_name,
            reason=f"Field '{col_name}' is present",
            evidence_snippet=cell.evidence_snippet,
            evidence_source_url=cell.source_url,
            is_hard=spec.is_hard,
        )

    # ── location ──────────────────────────────────────────────────────────────
    if spec.kind == "location":
        norm_target = _normalize_location(target)
        for col_name, cell in candidate_cells:
            if _location_matches(cell.value, norm_target):
                return RequirementMatch(
                    requirement_id=spec.id,
                    label=spec.label,
                    status="satisfied",
                    confidence=cell.confidence,
                    matched_value=cell.value,
                    matched_column=col_name,
                    reason=f"'{cell.value}' matches location '{spec.target_value_raw or target}'",
                    evidence_snippet=cell.evidence_snippet,
                    evidence_source_url=cell.source_url,
                    is_hard=spec.is_hard,
                )
        # Fields present but no match
        first_col, first_cell = candidate_cells[0]
        return RequirementMatch(
            requirement_id=spec.id,
            label=spec.label,
            status="not_satisfied",
            confidence=first_cell.confidence,
            matched_value=first_cell.value,
            matched_column=first_col,
            reason=f"'{first_cell.value}' does not match location '{spec.target_value_raw or target}'",
            evidence_snippet=first_cell.evidence_snippet,
            evidence_source_url=first_cell.source_url,
            is_hard=spec.is_hard,
        )

    # ── numeric ───────────────────────────────────────────────────────────────
    if spec.kind == "numeric":
        is_money = any(k in (spec.id or "") for k in ("fund", "val", "rais"))
        for col_name, cell in candidate_cells:
            result = _numeric_compare(cell.value, operator, target, money=is_money)
            if result is None:
                continue  # unparseable cell — try next
            status = "satisfied" if result else "not_satisfied"
            reason = (
                f"'{cell.value}' {'satisfies' if result else 'does not satisfy'} "
                f"requirement {operator.replace('_',' ')} {spec.target_value_raw or target}"
            )
            return RequirementMatch(
                requirement_id=spec.id,
                label=spec.label,
                status=status,
                confidence=cell.confidence,
                matched_value=cell.value,
                matched_column=col_name,
                reason=reason,
                evidence_snippet=cell.evidence_snippet,
                evidence_source_url=cell.source_url,
                is_hard=spec.is_hard,
            )
        # No cell yielded a parseable number
        first_col, first_cell = candidate_cells[0]
        return RequirementMatch(
            requirement_id=spec.id,
            label=spec.label,
            status="unknown",
            confidence=0.0,
            matched_value=first_cell.value,
            matched_column=first_col,
            reason=f"Could not parse numeric value from '{first_cell.value}'",
            evidence_snippet=first_cell.evidence_snippet,
            evidence_source_url=first_cell.source_url,
            is_hard=spec.is_hard,
        )

    # ── categorical / semantic / default ─────────────────────────────────────
    for col_name, cell in candidate_cells:
        cell_lower = cell.value.lower()
        target_lower = target.lower()
        if operator in ("equals",):
            matched = cell_lower == target_lower or target_lower in cell_lower
        elif operator in ("contains", "matches_topic"):
            matched = target_lower in cell_lower
        else:
            matched = target_lower in cell_lower  # fallback to substring

        if matched:
            return RequirementMatch(
                requirement_id=spec.id,
                label=spec.label,
                status="satisfied",
                confidence=cell.confidence,
                matched_value=cell.value,
                matched_column=col_name,
                reason=f"'{cell.value}' contains '{spec.target_value_raw or target}'",
                evidence_snippet=cell.evidence_snippet,
                evidence_source_url=cell.source_url,
                is_hard=spec.is_hard,
            )

    # Fields present but no string match
    first_col, first_cell = candidate_cells[0]
    return RequirementMatch(
        requirement_id=spec.id,
        label=spec.label,
        status="not_satisfied",
        confidence=first_cell.confidence,
        matched_value=first_cell.value,
        matched_column=first_col,
        reason=f"'{first_cell.value}' does not contain '{spec.target_value_raw or target}'",
        evidence_snippet=first_cell.evidence_snippet,
        evidence_source_url=first_cell.source_url,
        is_hard=spec.is_hard,
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
        matches.append(match)
        if match.status == "satisfied":
            sat += 1
            if spec.is_hard:
                hard_sat += 1
        elif match.status == "not_satisfied":
            not_sat += 1
        else:
            unk += 1

    denominator = sat + not_sat
    satisfaction_ratio = (sat / denominator) if denominator > 0 else 1.0

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

    log.debug(
        "Requirement summaries attached: %d rows, %d specs",
        len(rows),
        len(specs),
    )
    return rows
