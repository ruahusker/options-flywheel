"""Fetch live market data once and cache it, then rebuild every precomputed page.

This is the only component that calls the live provider (Tradier). The web path reads the cached
rows through CachedProvider, so page loads make no external calls. Invoked by the scheduled job
(`scripts/refresh_market_data.py`) on a 15-minute cadence during market hours.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.market_data import MarketDataCache
from app.services import precompute
from app.services.iv_history import record_atm_iv
from app.services.market_data.base import MarketDataProvider
from app.services.market_data.cached_provider import (
    KIND_CHAIN,
    KIND_EXPIRATIONS,
    KIND_HISTORY,
    KIND_QUOTE,
    KIND_STATUS,
    CachedProvider,
    latest_refresh_at,
)


SYMBOLS = ("IBIT", "ASST")
HISTORY_DAYS = 120
# Number of front expirations to cache chains for — covers the roll window and the pages that read
# the front month, without fetching every far-dated weekly.
CHAIN_EXPIRATIONS = 6
# Market clock states in which it's worth fetching (postmarket gives the post-close snapshot).
FETCHABLE_STATES = {"open", "postmarket"}
STATUS_KEY = "__market__"


def _upsert(db: Session, symbol: str, kind: str, key: str, payload_json: str, now: datetime) -> None:
    row = db.query(MarketDataCache).filter_by(symbol=symbol, kind=kind, key=key).one_or_none()
    if row is not None:
        row.payload_json = payload_json
        row.refreshed_at = now
    else:
        db.add(MarketDataCache(symbol=symbol, kind=kind, key=key, payload_json=payload_json, refreshed_at=now))


def fetch_and_cache(db: Session, provider: MarketDataProvider, symbols=SYMBOLS, chain_count: int = CHAIN_EXPIRATIONS) -> dict:
    """Pull quote/history/expirations/front chains for each symbol into MarketDataCache. Also records
    the daily ATM IV (moved here from page loads). Returns a per-symbol summary."""
    now = datetime.utcnow()
    status = provider.get_market_status()
    _upsert(db, STATUS_KEY, KIND_STATUS, "", status.model_dump_json(), now)

    summary: dict[str, dict] = {}
    for symbol in symbols:
        quote = provider.get_quote(symbol)
        _upsert(db, symbol, KIND_QUOTE, "", quote.model_dump_json(), now)

        bars = provider.get_price_history(symbol, HISTORY_DAYS, "1d")
        _upsert(db, symbol, KIND_HISTORY, "", json.dumps([b.model_dump(mode="json") for b in bars]), now)

        expirations = provider.get_option_expirations(symbol)
        _upsert(db, symbol, KIND_EXPIRATIONS, "", json.dumps([e.isoformat() for e in expirations]), now)

        front = expirations[:chain_count]
        front_chain = None
        for expiration in front:
            chain = provider.get_option_chain(symbol, expiration)
            _upsert(db, symbol, KIND_CHAIN, expiration.isoformat(), json.dumps([c.model_dump(mode="json") for c in chain]), now)
            if front_chain is None:
                front_chain = chain
        if front_chain:
            try:
                record_atm_iv(db, symbol, front_chain, quote.price)
            except Exception:
                pass
        summary[symbol] = {"bars": len(bars), "expirations": len(expirations), "chains_cached": len(front)}

    db.commit()
    return {"market_status": status.status, "symbols": summary}


def run_refresh(db: Session, provider: MarketDataProvider, *, force: bool = False) -> dict:
    """Gate on the market clock (unless forced), cache market data, then rebuild precomputed pages."""
    status = provider.get_market_status()
    if not force and status.status not in FETCHABLE_STATES:
        return {"skipped": True, "market_status": status.status}

    market = fetch_and_cache(db, provider)
    precompute_errors = precompute.refresh_all(db, CachedProvider(), market_refreshed_at=latest_refresh_at())
    return {"skipped": False, "market": market, "precompute_errors": precompute_errors}
