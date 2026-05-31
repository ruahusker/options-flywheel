from __future__ import annotations

from fastapi import APIRouter, Request

from app.routers.common import templates
from app.services.indicators import calculate_indicators
from app.services.market_data import get_provider


router = APIRouter(prefix="/indicators", tags=["indicators"])


@router.get("")
def indicators_page(request: Request):
    provider = get_provider()
    results = []
    warnings: list[str] = []
    for symbol in ("IBIT", "ASST"):
        try:
            bars = provider.get_price_history(symbol, 90, "1d")
            results.append(calculate_indicators(symbol, bars))
        except Exception as exc:
            warnings.append(f"{symbol}: {exc}")
    return templates.TemplateResponse(request, "indicators.html", {"results": results, "warnings": warnings})
