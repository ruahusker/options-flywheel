from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.iv_history import IVHistory
from app.schemas.market_data import OptionContractSchema

MIN_OBSERVATIONS_FOR_RANK = 20


def _atm_iv(chain: list[OptionContractSchema], underlying_price: float | None) -> float | None:
    """Implied vol of the call nearest the money — a stable daily IV reading for the symbol."""
    if not underlying_price or underlying_price <= 0:
        return None
    best_iv: float | None = None
    best_distance = float("inf")
    for option in chain:
        if option.option_type != "call" or not option.implied_volatility or option.implied_volatility <= 0:
            continue
        distance = abs(option.strike - underlying_price)
        if distance < best_distance:
            best_distance = distance
            best_iv = float(option.implied_volatility)
    return best_iv


def record_atm_iv(db: Session, symbol: str, chain: list[OptionContractSchema], underlying_price: float | None, observed_on: date | None = None) -> float | None:
    """Persist today's ATM IV for the symbol (one row per day; later pulls overwrite it)."""
    iv = _atm_iv(chain, underlying_price)
    if iv is None:
        return None
    observed_on = observed_on or date.today()
    existing = db.execute(
        select(IVHistory).where(IVHistory.symbol == symbol, IVHistory.observed_on == observed_on)
    ).scalar_one_or_none()
    if existing is None:
        db.add(IVHistory(symbol=symbol, observed_on=observed_on, atm_iv=iv))
    else:
        existing.atm_iv = iv
    db.commit()
    return iv


def iv_rank_for_symbol(db: Session, symbol: str, current_iv: float | None, lookback_days: int = 365) -> float | None:
    """IV rank in [0, 1]: where current IV sits within its trailing min/max range.

    Returns None until there are enough observations to be meaningful.
    """
    if current_iv is None or current_iv <= 0:
        return None
    cutoff = date.today() - timedelta(days=lookback_days)
    rows = db.execute(
        select(IVHistory.atm_iv).where(IVHistory.symbol == symbol, IVHistory.observed_on >= cutoff)
    ).scalars().all()
    values = [float(v) for v in rows if v and v > 0]
    if len(values) < MIN_OBSERVATIONS_FOR_RANK:
        return None
    low = min(values)
    high = max(values)
    if high <= low:
        return None
    rank = (current_iv - low) / (high - low)
    return max(0.0, min(1.0, rank))
