from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services.performance import compute_performance, list_available_snapshots
from app.services.trade_ledger import build_trade_rounds, journal_date_span, rounds_in_range, summarize_range

router = APIRouter(prefix="/performance", tags=["performance"])


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.get("")
def performance_page(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
    db: Session = Depends(get_db),
):
    span = journal_date_span(db)
    available = list_available_snapshots(db)

    # Default end: the newest data point we have — last journal trade or last snapshot —
    # so the default view always includes the most recent import.
    candidates = [s["created_at"] for s in available[-1:]] + ([span[1]] if span else [])
    default_end = max(candidates) if candidates else datetime.utcnow()
    end_dt = _parse_date(end) or default_end
    if days:
        start_dt = end_dt - timedelta(days=days - 1)
    else:
        start_dt = _parse_date(start) or (span[0] if span else end_dt - timedelta(days=29))
    if start_dt > end_dt:
        start_dt = end_dt

    # Chart baseline: last snapshot at/before the range start (so the B&H line starts where the
    # range starts), ending at the last snapshot inside the range.
    end_of_day = end_dt + timedelta(days=1)
    before_start = [s for s in available if s["created_at"] <= start_dt]
    in_range = [s for s in available if s["created_at"] < end_of_day]
    start_snap = before_start[-1] if before_start else (available[0] if available else None)
    end_snap = in_range[-1] if in_range else None
    result = compute_performance(
        db,
        start_id=start_snap["id"] if start_snap else None,
        end_id=end_snap["id"] if end_snap else None,
    )
    base_value = result.checkpoints[0]["strategy_value"] if result.checkpoints else None

    rounds = build_trade_rounds(db)
    stats = summarize_range(db, rounds, start_dt, end_dt, base_value=base_value)
    trades = rounds_in_range(rounds, start_dt, end_dt)

    payload = {
        "start_value": start_dt.date().isoformat(),
        "end_value": end_dt.date().isoformat(),
        "stats": stats,
        "trades": trades,
        "checkpoints": result.checkpoints,
        "summary": result.summary,
        "warnings": result.warnings,
        "has_data": bool(trades or result.checkpoints),
    }
    return templates.TemplateResponse(request, "performance.html", payload)
