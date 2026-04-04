"""
Export routes.

GET /api/export/json?query_id=...  → full JSON response
GET /api/export/csv?query_id=...   → flattened CSV with provenance columns
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.models.db import get_job
from app.models.schema import SearchResponse
from app.services.exporter import to_csv, to_json

router = APIRouter()


async def _load_result(query_id: str) -> SearchResponse:
    row = await get_job(query_id)
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")
    if row["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Query is not done yet (status={row['status']})")
    if not row.get("result_json"):
        raise HTTPException(status_code=500, detail="Result data missing")
    return SearchResponse(**json.loads(row["result_json"]))


@router.get("/export/json")
async def export_json(query_id: str) -> Response:
    result = await _load_result(query_id)
    content = to_json(result)
    filename = f"search_{query_id[:8]}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/csv")
async def export_csv(query_id: str) -> Response:
    result = await _load_result(query_id)
    content = to_csv(result)
    filename = f"search_{query_id[:8]}.csv"
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
