from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute
from app.services.market_data import get_provider


router = APIRouter(prefix="/live-data", tags=["live-data"])


@router.get("")
def live_data(
    request: Request,
    symbol: str = Query("IBIT"),
    expiration: str | None = Query(None),
    db: Session = Depends(get_db),
):
    # The default IBIT view is precomputed; any other symbol/expiration is built live against the
    # cached market data (CachedProvider) — still no external calls.
    if symbol.upper() == "IBIT" and expiration is None:
        payload = precompute.load_or_build("live_data", db)
    else:
        payload = precompute.build_live_data(db, get_provider(), symbol, expiration)
    return templates.TemplateResponse(request, "live_data.html", payload)
