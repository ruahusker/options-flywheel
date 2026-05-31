from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services.historical_backtest import build_historical_readiness, focused_backfill_preview


router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("")
def backtest_page(request: Request, db: Session = Depends(get_db)):
    readiness = build_historical_readiness(db)
    preview = focused_backfill_preview(db)
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "readiness": readiness,
            "preview": preview,
            "warnings": [],
        },
    )
