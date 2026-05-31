from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.market_data import HistoricalOptionContract, OptionPriceBar, PriceHistory
from app.services.massive_backfill import query_focused_contracts_for_bar_backfill


@dataclass(frozen=True)
class CacheCoverageRow:
    symbol: str
    underlying_bars: int
    underlying_start: date | None
    underlying_end: date | None
    contracts: int
    contract_start: date | None
    contract_end: date | None
    option_contracts_with_bars: int
    option_bars: int
    attempted_contracts: int
    focused_remaining: int


@dataclass(frozen=True)
class RegimeCoverageRow:
    symbol: str
    bars: int
    latest_close: float | None
    latest_rsi_14: float | None
    overbought_days: int
    oversold_days: int
    bullish_days: int
    bearish_days: int
    neutral_days: int


@dataclass(frozen=True)
class HistoricalReadiness:
    rows: list[CacheCoverageRow]
    regimes: list[RegimeCoverageRow]
    focused_remaining_total: int
    estimated_days_for_focused_options: float | None
    calls_per_day: int
    recommendation: str


def build_historical_readiness(
    db: Session,
    *,
    underlyings: tuple[str, ...] = ("IBIT", "ASST"),
    start: date | None = None,
    end: date | None = None,
    calls_per_chunk: int = 5,
    chunk_interval_minutes: int = 15,
) -> HistoricalReadiness:
    end = end or date.today()
    start = start or end - timedelta(days=730)
    rows = [_coverage_for_symbol(db, symbol, start=start, end=end) for symbol in underlyings]
    regimes = [_regime_coverage_for_symbol(db, symbol) for symbol in underlyings]
    focused_remaining_total = sum(row.focused_remaining for row in rows)
    calls_per_day = int((24 * 60 / chunk_interval_minutes) * calls_per_chunk)
    estimated_days = focused_remaining_total / calls_per_day if calls_per_day and focused_remaining_total else 0.0
    recommendation = _recommendation(rows, regimes)
    return HistoricalReadiness(
        rows=rows,
        regimes=regimes,
        focused_remaining_total=focused_remaining_total,
        estimated_days_for_focused_options=estimated_days,
        calls_per_day=calls_per_day,
        recommendation=recommendation,
    )


def focused_backfill_preview(
    db: Session,
    *,
    underlyings: tuple[str, ...] = ("IBIT", "ASST"),
    start: date | None = None,
    end: date | None = None,
    limit: int = 12,
) -> list[HistoricalOptionContract]:
    end = end or date.today()
    start = start or end - timedelta(days=730)
    return query_focused_contracts_for_bar_backfill(db, underlyings, start=start, end=end, max_rows=limit)


def _coverage_for_symbol(db: Session, symbol: str, *, start: date, end: date) -> CacheCoverageRow:
    symbol = symbol.upper()
    underlying = (
        db.query(func.count(PriceHistory.id), func.min(PriceHistory.date_time), func.max(PriceHistory.date_time))
        .filter(PriceHistory.provider == "massive", PriceHistory.symbol == symbol, PriceHistory.interval == "1d")
        .one()
    )
    contracts = (
        db.query(
            func.count(HistoricalOptionContract.id),
            func.min(HistoricalOptionContract.expiration),
            func.max(HistoricalOptionContract.expiration),
        )
        .filter(HistoricalOptionContract.provider == "massive", HistoricalOptionContract.underlying == symbol)
        .one()
    )
    option_bars = (
        db.query(func.count(OptionPriceBar.id), func.count(func.distinct(OptionPriceBar.option_symbol)))
        .filter(OptionPriceBar.provider == "massive", OptionPriceBar.underlying == symbol, OptionPriceBar.interval == "1d")
        .one()
    )
    attempted = (
        db.query(func.count(HistoricalOptionContract.id))
        .filter(
            HistoricalOptionContract.provider == "massive",
            HistoricalOptionContract.underlying == symbol,
            HistoricalOptionContract.bars_fetched_interval == "1d",
        )
        .scalar()
        or 0
    )
    focused_remaining = len(query_focused_contracts_for_bar_backfill(db, [symbol], start=start, end=end))
    return CacheCoverageRow(
        symbol=symbol,
        underlying_bars=int(underlying[0] or 0),
        underlying_start=_as_date(underlying[1]),
        underlying_end=_as_date(underlying[2]),
        contracts=int(contracts[0] or 0),
        contract_start=contracts[1],
        contract_end=contracts[2],
        option_contracts_with_bars=int(option_bars[1] or 0),
        option_bars=int(option_bars[0] or 0),
        attempted_contracts=int(attempted),
        focused_remaining=focused_remaining,
    )


def _regime_coverage_for_symbol(db: Session, symbol: str) -> RegimeCoverageRow:
    bars = (
        db.query(PriceHistory.date_time, PriceHistory.close)
        .filter(PriceHistory.provider == "massive", PriceHistory.symbol == symbol.upper(), PriceHistory.interval == "1d")
        .order_by(PriceHistory.date_time.asc())
        .all()
    )
    closes = [float(close) for _, close in bars if close is not None]
    counts = {"overbought": 0, "oversold": 0, "bullish": 0, "bearish": 0, "neutral": 0}
    latest_rsi: float | None = None
    for index, close in enumerate(closes):
        rsi = _rsi(closes, index)
        if rsi is not None:
            latest_rsi = rsi
        if index < 50:
            continue
        sma20 = sum(closes[index - 19 : index + 1]) / 20
        sma50 = sum(closes[index - 49 : index + 1]) / 50
        high20 = max(closes[index - 19 : index + 1])
        if rsi is not None and rsi >= 70 and close >= high20 * 0.98:
            counts["overbought"] += 1
        elif rsi is not None and rsi <= 35:
            counts["oversold"] += 1
        elif close > sma20 and close > sma50:
            counts["bullish"] += 1
        elif close < sma20 and close < sma50:
            counts["bearish"] += 1
        else:
            counts["neutral"] += 1
    return RegimeCoverageRow(
        symbol=symbol.upper(),
        bars=len(closes),
        latest_close=closes[-1] if closes else None,
        latest_rsi_14=latest_rsi,
        overbought_days=counts["overbought"],
        oversold_days=counts["oversold"],
        bullish_days=counts["bullish"],
        bearish_days=counts["bearish"],
        neutral_days=counts["neutral"],
    )


def _rsi(closes: list[float], index: int, period: int = 14) -> float | None:
    if index < period:
        return None
    gains = 0.0
    losses = 0.0
    for cursor in range(index - period + 1, index + 1):
        change = closes[cursor] - closes[cursor - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _recommendation(rows: list[CacheCoverageRow], regimes: list[RegimeCoverageRow]) -> str:
    if any(row.underlying_bars < 200 for row in rows):
        return "Finish underlying history first; technical-regime testing needs a deep price series."
    if sum(row.option_contracts_with_bars for row in rows) < 50:
        return "Keep running the focused option-bar backfill before trusting option premium results."
    if sum(row.focused_remaining for row in rows) > 0:
        return "Use current results as preliminary and continue focused backfill for the remaining strategy-relevant contracts."
    if any(regime.overbought_days == 0 or regime.oversold_days == 0 for regime in regimes):
        return "Option data is usable, but regime coverage is uneven; avoid overfitting one market state."
    return "Enough focused data is cached to start comparing 25%, 35%, and 50% sleeves by regime."


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return value.date()
