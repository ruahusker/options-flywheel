from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, Request

from app.routers.common import templates
from app.services.market_data import get_provider


router = APIRouter(prefix="/live-data", tags=["live-data"])


@router.get("")
def live_data(request: Request, symbol: str = Query("IBIT"), expiration: str | None = Query(None)):
    provider = get_provider()
    warnings: list[str] = []
    quote = None
    expirations = []
    chain = []
    status = provider.get_market_status()
    try:
        quote = provider.get_quote(symbol)
        expirations = provider.get_option_expirations(symbol)
        selected = date.fromisoformat(expiration) if expiration else (expirations[0] if expirations else None)
        if selected:
            chain = provider.get_option_chain(symbol, selected)
    except Exception as exc:
        warnings.append(str(exc))
    return templates.TemplateResponse(
        request,
        "live_data.html",
        {
            "symbol": symbol.upper(),
            "quote": quote,
            "expirations": expirations,
            "selected_expiration": expiration,
            "chain": chain,
            "status": status,
            "warnings": warnings,
        },
    )
