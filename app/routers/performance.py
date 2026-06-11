from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services.performance import compute_performance, list_available_snapshots

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("")
def performance_page(
    request: Request,
    start: int | None = None,
    end: int | None = None,
    db: Session = Depends(get_db),
):
    available = list_available_snapshots(db)
    result = compute_performance(db, start_id=start, end_id=end)

    # Build a payload the template can use directly (lists of dicts + summary)
    payload = {
        "available_snapshots": available,
        "selected_start": start,
        "selected_end": end,
        "checkpoints": result.checkpoints,
        "summary": result.summary,
        "journal_premiums": result.journal_premiums,
        "warnings": result.warnings,
        "has_data": bool(result.checkpoints),
    }
    return templates.TemplateResponse(request, "performance.html", payload)
