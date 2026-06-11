"""Fetch live market data once and cache it, then rebuild every precomputed page.

This is the only component that calls the live provider (Tradier). The web path reads the cached
rows through CachedProvider, so page loads make no external calls. Invoked by the scheduled job
(`scripts/refresh_market_data.py`) on a 15-minute cadence during market hours.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models.market_data import FocusedOptionSnapshot, MarketDataCache
from app.schemas.market_data import OptionContractSchema
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


PRIMARY_SYMBOLS = ("IBIT", "ASST")
REFERENCE_SYMBOLS = ("QQQ", "IWM", "MSTR", "XXI", "SPY", "BSOL", "ETHA")
SYMBOLS = PRIMARY_SYMBOLS + REFERENCE_SYMBOLS
HISTORY_DAYS = 120
# Number of front expirations to cache chains for — covers the roll window and the pages that read
# the front month, without fetching every far-dated weekly.
CHAIN_EXPIRATIONS = 6
# Reference-only symbols are not used by the current roll calculations, so cache fewer chains to
# build useful context without making every refresh pull large SPY/QQQ/IWM option chains.
REFERENCE_CHAIN_EXPIRATIONS = 2
# Retained intraday option/Greek rows per symbol per 15-minute bucket.
FOCUSED_OPTION_SNAPSHOT_LIMIT = 40
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
        symbol_summary = {"bars": 0, "expirations": 0, "chains_cached": 0, "focused_snapshots": 0}
        warnings: list[str] = []
        try:
            quote = provider.get_quote(symbol)
            _upsert(db, symbol, KIND_QUOTE, "", quote.model_dump_json(), now)

            bars = provider.get_price_history(symbol, HISTORY_DAYS, "1d")
            _upsert(db, symbol, KIND_HISTORY, "", json.dumps([b.model_dump(mode="json") for b in bars]), now)
            symbol_summary["bars"] = len(bars)

            try:
                expirations = provider.get_option_expirations(symbol)
            except Exception as exc:
                warnings.append(f"option expirations unavailable: {exc}")
                expirations = []
            _upsert(db, symbol, KIND_EXPIRATIONS, "", json.dumps([e.isoformat() for e in expirations]), now)
            symbol_summary["expirations"] = len(expirations)

            symbol_chain_count = _chain_count_for_symbol(symbol, chain_count)
            front = expirations[:symbol_chain_count]
            front_chain = None
            focused_snapshot_candidates: list[OptionContractSchema] = []
            for expiration in front:
                try:
                    chain = provider.get_option_chain(symbol, expiration)
                except Exception as exc:
                    warnings.append(f"{expiration} chain unavailable: {exc}")
                    continue
                _upsert(db, symbol, KIND_CHAIN, expiration.isoformat(), json.dumps([c.model_dump(mode="json") for c in chain]), now)
                focused_snapshot_candidates.extend(chain)
                symbol_summary["chains_cached"] += 1
                if front_chain is None:
                    front_chain = chain
            symbol_summary["focused_snapshots"] = record_focused_option_snapshots(
                db,
                symbol,
                focused_snapshot_candidates,
                underlying_price=quote.price,
                captured_at=now,
                provider_name=provider.name,
                market_status=status.status,
            )
            if front_chain:
                try:
                    record_atm_iv(db, symbol, front_chain, quote.price)
                except Exception as exc:
                    warnings.append(f"ATM IV not recorded: {exc}")
        except Exception as exc:
            warnings.append(f"symbol refresh failed: {exc}")
        if warnings:
            symbol_summary["warnings"] = warnings
        summary[symbol] = symbol_summary

    db.commit()
    return {"market_status": status.status, "symbols": summary}


def _chain_count_for_symbol(symbol: str, chain_count: int) -> int:
    if chain_count != CHAIN_EXPIRATIONS:
        return chain_count
    if symbol.upper() in REFERENCE_SYMBOLS:
        return REFERENCE_CHAIN_EXPIRATIONS
    return chain_count


def record_focused_option_snapshots(
    db: Session,
    symbol: str,
    chain: list[OptionContractSchema],
    *,
    underlying_price: float | None,
    captured_at: datetime,
    provider_name: str,
    market_status: str,
    limit: int = FOCUSED_OPTION_SNAPSHOT_LIMIT,
) -> int:
    """Retain a focused intraday slice of option quotes/Greeks for later analysis.

    Rows are bucketed to the 15-minute refresh slot and upserted by provider symbol so force-runs in
    the same slot update the snapshot instead of duplicating it.
    """
    selected = _focused_option_candidates(chain, underlying_price=underlying_price, limit=limit)
    bucket = _snapshot_bucket(captured_at)
    count = 0
    for option in selected:
        provider_symbol = option.provider_symbol or _fallback_option_symbol(option)
        bid = option.bid
        ask = option.ask
        mid = option.mid if option.mid is not None else ((bid + ask) / 2 if bid is not None and ask is not None else option.last)
        spread = max(ask - bid, 0.0) if bid is not None and ask is not None else None
        spread_pct = spread / mid if spread is not None and mid and mid > 0 else None
        moneyness = option.strike / underlying_price if underlying_price and underlying_price > 0 else None
        existing = (
            db.query(FocusedOptionSnapshot)
            .filter_by(provider=provider_name, provider_symbol=provider_symbol, captured_at=bucket)
            .one_or_none()
        )
        values = {
            "provider": provider_name,
            "provider_symbol": provider_symbol,
            "underlying": symbol.upper(),
            "captured_at": bucket,
            "expiration": option.expiration,
            "option_type": option.option_type,
            "strike": option.strike,
            "dte": option.dte,
            "underlying_price": underlying_price,
            "moneyness": moneyness,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": option.last,
            "spread": spread,
            "spread_pct": spread_pct,
            "volume": option.volume,
            "open_interest": option.open_interest,
            "implied_volatility": option.implied_volatility,
            "delta": option.delta,
            "gamma": option.gamma,
            "theta": option.theta,
            "vega": option.vega,
            "liquidity_score": option.liquidity_score,
            "market_status": option.market_status or market_status,
            "is_stale": option.is_stale,
            "fetched_at": captured_at,
        }
        if existing is None:
            db.add(FocusedOptionSnapshot(**values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        count += 1
    return count


def _focused_option_candidates(
    chain: list[OptionContractSchema],
    *,
    underlying_price: float | None,
    limit: int,
) -> list[OptionContractSchema]:
    scored: list[tuple[float, date, str, float, OptionContractSchema]] = []
    for option in chain:
        score = _focused_option_score(option, underlying_price)
        if score is None:
            continue
        scored.append((score, option.expiration, option.option_type, option.strike, option))
    scored.sort(key=lambda row: row[:4])
    return [row[4] for row in scored[:limit]]


def _focused_option_score(option: OptionContractSchema, underlying_price: float | None) -> float | None:
    option_type = option.option_type.lower()
    dte = option.dte if option.dte is not None else max((option.expiration - date.today()).days, 0)
    if dte > 45:
        return None
    if option.delta is not None:
        abs_delta = abs(float(option.delta))
        if not 0.10 <= abs_delta <= 0.60:
            return None
        if option_type not in {"call", "put"}:
            return None
        target_delta = 0.30 if option_type == "call" else 0.35
        score = abs(abs_delta - target_delta)
    elif underlying_price and underlying_price > 0:
        ratio = option.strike / underlying_price
        if option_type == "call":
            if not 1.00 <= ratio <= 1.25:
                return None
            score = abs(ratio - 1.08)
        elif option_type == "put":
            if not 0.75 <= ratio <= 1.02:
                return None
            score = abs(ratio - 0.94)
        else:
            return None
    else:
        return None

    if option.bid is not None and option.ask is not None:
        mid = option.mid if option.mid is not None else (option.bid + option.ask) / 2
        if mid and mid > 0:
            score += min(max((option.ask - option.bid) / mid, 0.0), 1.0) * 0.05
    score += min(max(dte, 0), 45) / 1000
    return score


def _snapshot_bucket(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _fallback_option_symbol(option: OptionContractSchema) -> str:
    option_code = "C" if option.option_type.lower() == "call" else "P"
    strike = int(round(option.strike * 1000))
    return f"{option.underlying}{option.expiration:%y%m%d}{option_code}{strike:08d}"


def run_refresh(db: Session, provider: MarketDataProvider, *, force: bool = False) -> dict:
    """Gate on the market clock (unless forced), cache market data, then rebuild precomputed pages."""
    status = provider.get_market_status()
    if not force and status.status not in FETCHABLE_STATES:
        return {"skipped": True, "market_status": status.status}

    market = fetch_and_cache(db, provider)
    precompute_errors = precompute.refresh_all(db, CachedProvider(), market_refreshed_at=latest_refresh_at())
    return {"skipped": False, "market": market, "precompute_errors": precompute_errors}
