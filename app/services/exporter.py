"""
Export helpers: JSON passthrough and flattened CSV.

CSV format:
  - One row per entity
  - For each column: <col>   <col>_source_url
  - Plus: entity_id, sources_count, aggregate_confidence
"""

from __future__ import annotations

import csv
import io
import json

from app.models.schema import SearchResponse


def to_json(response: SearchResponse) -> str:
    """Return the full SearchResponse as a pretty-printed JSON string."""
    return json.dumps(response.model_dump(), indent=2, ensure_ascii=False)


def to_csv(response: SearchResponse) -> str:
    """Return a flattened CSV string with provenance columns."""
    columns = response.columns

    # Build header
    header = ["entity_id"]
    for col in columns:
        header.append(col)
        header.append(f"{col}_source_url")
        header.append(f"{col}_confidence")
    header += ["sources_count", "aggregate_confidence"]

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)

    for row in response.rows:
        record = [row.entity_id]
        for col in columns:
            cell = row.cells.get(col)
            if cell:
                record.append(cell.value)
                record.append(cell.source_url)
                record.append(str(round(cell.confidence, 2)))
            else:
                record.extend(["", "", ""])
        record.append(str(row.sources_count))
        record.append(str(row.aggregate_confidence))
        writer.writerow(record)

    return output.getvalue()
