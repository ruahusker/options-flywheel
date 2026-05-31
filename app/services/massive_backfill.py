from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.market_data import HistoricalOptionContract, OptionPriceBar, PriceHistory
from app.services.massive_client import MassiveBar, MassiveCallBudgetExhausted, MassiveClient, MassiveOptionContract


@dataclass
class MassiveBackfillResult:
    calls_made: int = 0
    contracts_seen: int = 0
    contracts_inserted: int = 0
    contracts_updated: int = 0
    option_bars_seen: int = 0
    option_bars_inserted: int = 0
    option_bars_updated: int = 0
    stock_bars_seen: int = 0
    stock_bars_inserted: int = 0
    stock_bars_updated: int = 0
    stopped_reason: str | None = None

    def absorb(self, other: MassiveBackfillResult) -> None:
        self.calls_made = other.calls_made
        self.contracts_seen += other.contracts_seen
        self.contracts_inserted += other.contracts_inserted
        self.contracts_updated += other.contracts_updated
        self.option_bars_seen += other.option_bars_seen
        self.option_bars_inserted += other.option_bars_inserted
        self.option_bars_updated += other.option_bars_updated
        self.stock_bars_seen += other.stock_bars_seen
        self.stock_bars_inserted += other.stock_bars_inserted
        self.stock_bars_updated += other.stock_bars_updated
        self.stopped_reason = other.stopped_reason or self.stopped_reason


class MassiveBackfillService:
    provider = "massive"

    def __init__(self, client: MassiveClient):
        self.client = client

    def backfill_underlying_bars(
        self,
        db: Session,
        underlyings: Iterable[str],
        *,
        start: date,
        end: date,
        interval: str = "1d",
        resume_from_latest: bool = True,
        refresh_lookback_days: int = 7,
    ) -> MassiveBackfillResult:
        result = MassiveBackfillResult()
        try:
            for underlying in _unique_upper(underlyings):
                effective_start = start
                latest_bar = latest_stock_bar_date(db, underlying, interval=interval, provider=self.provider)
                if resume_from_latest and latest_bar is not None:
                    effective_start = max(start, latest_bar - timedelta(days=refresh_lookback_days))
                bars = self.client.get_stock_bars(underlying, effective_start, end, timespan=_massive_timespan(interval))
                result.stock_bars_seen += len(bars)
                inserted, updated = upsert_stock_bars(db, bars, interval=interval, provider=self.provider)
                result.stock_bars_inserted += inserted
                result.stock_bars_updated += updated
                db.commit()
        except MassiveCallBudgetExhausted as exc:
            result.stopped_reason = str(exc)
        result.calls_made = self.client.calls_made
        return result

    def backfill_contracts(
        self,
        db: Session,
        underlyings: Iterable[str],
        *,
        as_of: date | None = None,
        start: date | None = None,
        end: date | None = None,
        include_expired: bool = True,
        include_active: bool = True,
        resume_from_latest_expiration: bool = True,
    ) -> MassiveBackfillResult:
        result = MassiveBackfillResult()
        expired_flags: list[bool | None] = []
        if include_expired:
            expired_flags.append(True)
        if include_active:
            expired_flags.append(False)
        if not expired_flags:
            expired_flags.append(None)

        try:
            for underlying in _unique_upper(underlyings):
                effective_start = start
                latest_expiration = latest_contract_expiration(db, underlying, provider=self.provider)
                if resume_from_latest_expiration and latest_expiration is not None:
                    if end is not None and latest_expiration >= end:
                        continue
                    next_expiration_date = latest_expiration + timedelta(days=1)
                    if effective_start is None or next_expiration_date > effective_start:
                        effective_start = next_expiration_date
                for expired in expired_flags:
                    pages = self.client.iter_option_contract_pages(
                        underlying,
                        as_of=as_of,
                        expired=expired,
                        expiration_gte=effective_start,
                        expiration_lte=end,
                    )
                    for page in pages:
                        result.contracts_seen += len(page)
                        inserted, updated = upsert_contracts(db, page, as_of=as_of)
                        result.contracts_inserted += inserted
                        result.contracts_updated += updated
                        db.commit()
        except MassiveCallBudgetExhausted as exc:
            result.stopped_reason = str(exc)
        result.calls_made = self.client.calls_made
        return result

    def backfill_option_bars(
        self,
        db: Session,
        underlyings: Iterable[str],
        *,
        start: date,
        end: date,
        interval: str = "1d",
        dte_lookback_days: int = 21,
        max_contracts: int | None = None,
        refresh_existing: bool = False,
        focused: bool = False,
    ) -> MassiveBackfillResult:
        result = MassiveBackfillResult()
        contracts: list[HistoricalOptionContract] = []
        if focused:
            candidate_contracts = query_focused_contracts_for_bar_backfill(
                db,
                underlyings,
                start=start,
                end=end,
                interval=interval,
                dte_lookback_days=dte_lookback_days,
            )
        else:
            candidate_contracts = query_contracts_for_bar_backfill(db, underlyings, start=start, end=end)

        for contract in candidate_contracts:
            fetch_end = min(end, contract.expiration)
            if (
                not refresh_existing
                and contract.bars_fetched_interval == interval
                and contract.bars_fetched_through is not None
                and contract.bars_fetched_through >= fetch_end
            ):
                continue
            contracts.append(contract)
            if max_contracts is not None and len(contracts) >= max_contracts:
                break

        try:
            for contract in contracts:
                fetch_start = max(start, contract.expiration - timedelta(days=dte_lookback_days))
                fetch_end = min(end, contract.expiration)
                if (
                    not refresh_existing
                    and contract.bars_fetched_interval == interval
                    and contract.bars_fetched_through is not None
                ):
                    fetch_start = max(fetch_start, contract.bars_fetched_through + timedelta(days=1))
                if fetch_start > fetch_end:
                    continue
                bars = self.client.get_option_bars(
                    contract.provider_symbol,
                    fetch_start,
                    fetch_end,
                    timespan=_massive_timespan(interval),
                )
                result.option_bars_seen += len(bars)
                inserted, updated = upsert_option_bars(db, contract, bars, interval=interval, provider=self.provider)
                result.option_bars_inserted += inserted
                result.option_bars_updated += updated
                contract.bars_fetched_at = _utcnow()
                contract.bars_fetched_interval = interval
                contract.bars_fetched_through = fetch_end
                db.commit()
        except MassiveCallBudgetExhausted as exc:
            result.stopped_reason = str(exc)
        result.calls_made = self.client.calls_made
        return result


def upsert_contracts(
    db: Session,
    contracts: Iterable[MassiveOptionContract],
    *,
    as_of: date | None = None,
    provider: str = "massive",
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    now = _utcnow()
    pending: dict[str, tuple[HistoricalOptionContract, bool]] = {}
    for contract in contracts:
        existing, is_new = pending.get(contract.provider_symbol, (None, False))
        if existing is None:
            existing = (
                db.query(HistoricalOptionContract)
                .filter(
                    HistoricalOptionContract.provider == provider,
                    HistoricalOptionContract.provider_symbol == contract.provider_symbol,
                )
                .one_or_none()
            )
        if existing is None:
            existing = HistoricalOptionContract(
                provider=provider,
                provider_symbol=contract.provider_symbol,
                underlying=contract.underlying,
                expiration=contract.expiration,
                option_type=contract.option_type,
                strike=contract.strike,
                shares_per_contract=contract.shares_per_contract,
                exercise_style=contract.exercise_style,
                first_seen_as_of=as_of,
                last_seen_as_of=as_of,
                fetched_at=now,
            )
            db.add(existing)
            pending[contract.provider_symbol] = (existing, True)
            inserted += 1
            continue

        existing.underlying = contract.underlying
        existing.expiration = contract.expiration
        existing.option_type = contract.option_type
        existing.strike = contract.strike
        existing.shares_per_contract = contract.shares_per_contract
        existing.exercise_style = contract.exercise_style
        existing.last_seen_as_of = as_of or existing.last_seen_as_of
        existing.fetched_at = now
        pending[contract.provider_symbol] = (existing, is_new)
        if not is_new:
            updated += 1
    return inserted, updated


def upsert_option_bars(
    db: Session,
    contract: HistoricalOptionContract,
    bars: Iterable[MassiveBar],
    *,
    interval: str,
    provider: str = "massive",
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    now = _utcnow()
    for bar in bars:
        existing = (
            db.query(OptionPriceBar)
            .filter(
                OptionPriceBar.provider == provider,
                OptionPriceBar.option_symbol == contract.provider_symbol,
                OptionPriceBar.date_time == bar.date_time,
                OptionPriceBar.interval == interval,
            )
            .one_or_none()
        )
        if existing is None:
            db.add(
                OptionPriceBar(
                    provider=provider,
                    option_symbol=contract.provider_symbol,
                    underlying=contract.underlying,
                    expiration=contract.expiration,
                    option_type=contract.option_type,
                    strike=contract.strike,
                    date_time=bar.date_time,
                    interval=interval,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    vwap=bar.vwap,
                    transactions=bar.transactions,
                    fetched_at=now,
                )
            )
            inserted += 1
            continue

        existing.open = bar.open
        existing.high = bar.high
        existing.low = bar.low
        existing.close = bar.close
        existing.volume = bar.volume
        existing.vwap = bar.vwap
        existing.transactions = bar.transactions
        existing.fetched_at = now
        updated += 1
    return inserted, updated


def upsert_stock_bars(
    db: Session,
    bars: Iterable[MassiveBar],
    *,
    interval: str,
    provider: str = "massive",
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for bar in bars:
        existing = (
            db.query(PriceHistory)
            .filter(
                PriceHistory.provider == provider,
                PriceHistory.symbol == bar.symbol,
                PriceHistory.date_time == bar.date_time,
                PriceHistory.interval == interval,
            )
            .one_or_none()
        )
        if existing is None:
            db.add(
                PriceHistory(
                    provider=provider,
                    symbol=bar.symbol,
                    date_time=bar.date_time,
                    interval=interval,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
            )
            inserted += 1
            continue
        existing.open = bar.open
        existing.high = bar.high
        existing.low = bar.low
        existing.close = bar.close
        existing.volume = bar.volume
        updated += 1
    return inserted, updated


def query_contracts_for_bar_backfill(
    db: Session,
    underlyings: Iterable[str],
    *,
    start: date,
    end: date,
    provider: str = "massive",
) -> list[HistoricalOptionContract]:
    symbols = _unique_upper(underlyings)
    return (
        db.query(HistoricalOptionContract)
        .filter(
            HistoricalOptionContract.provider == provider,
            HistoricalOptionContract.underlying.in_(symbols),
            HistoricalOptionContract.expiration >= start,
            HistoricalOptionContract.expiration <= end,
        )
        .order_by(
            HistoricalOptionContract.expiration.asc(),
            HistoricalOptionContract.underlying.asc(),
            HistoricalOptionContract.option_type.asc(),
            HistoricalOptionContract.strike.asc(),
        )
        .all()
    )


def query_focused_contracts_for_bar_backfill(
    db: Session,
    underlyings: Iterable[str],
    *,
    start: date,
    end: date,
    interval: str = "1d",
    dte_lookback_days: int = 21,
    provider: str = "massive",
    max_rows: int | None = None,
) -> list[HistoricalOptionContract]:
    symbols = _unique_upper(underlyings)
    price_maps = {symbol: _underlying_price_lookup(db, symbol, interval=interval, provider=provider) for symbol in symbols}
    scored: list[tuple[date, float, HistoricalOptionContract]] = []
    for contract in query_contracts_for_bar_backfill(db, symbols, start=start, end=end, provider=provider):
        if (
            contract.bars_fetched_interval == interval
            and contract.bars_fetched_through is not None
            and contract.bars_fetched_through >= min(end, contract.expiration)
        ):
            continue
        entry_date = max(start, contract.expiration - timedelta(days=min(dte_lookback_days, 10)))
        spot = _price_on_or_before(price_maps.get(contract.underlying, []), entry_date)
        if not spot or spot <= 0:
            continue
        ratio = contract.strike / spot
        relevance = _strategy_moneyness_score(contract.option_type, ratio)
        if relevance is None:
            continue
        scored.append((contract.expiration, relevance, contract))
    scored.sort(key=lambda item: (item[0], item[1], item[2].underlying, item[2].option_type, item[2].strike))
    contracts = [contract for _, _, contract in scored]
    return contracts[:max_rows] if max_rows is not None else contracts


def _strategy_moneyness_score(option_type: str, strike_to_spot: float) -> float | None:
    if option_type == "call":
        if not 1.02 <= strike_to_spot <= 1.25:
            return None
        return abs(strike_to_spot - 1.10)
    if option_type == "put":
        if not 0.80 <= strike_to_spot <= 1.01:
            return None
        return abs(strike_to_spot - 0.94)
    return None


def _underlying_price_lookup(
    db: Session,
    symbol: str,
    *,
    interval: str,
    provider: str = "massive",
) -> list[tuple[date, float]]:
    rows = (
        db.query(PriceHistory.date_time, PriceHistory.close)
        .filter(
            PriceHistory.provider == provider,
            PriceHistory.symbol == symbol.upper(),
            PriceHistory.interval == interval,
        )
        .order_by(PriceHistory.date_time.asc())
        .all()
    )
    return [(date_time.date(), float(close)) for date_time, close in rows if close is not None]


def _price_on_or_before(price_rows: list[tuple[date, float]], target_date: date) -> float | None:
    if not price_rows:
        return None
    dates = [row[0] for row in price_rows]
    index = bisect_right(dates, target_date) - 1
    if index < 0:
        return None
    return price_rows[index][1]


def latest_contract_expiration(
    db: Session,
    underlying: str,
    *,
    provider: str = "massive",
) -> date | None:
    row = (
        db.query(HistoricalOptionContract.expiration)
        .filter(
            HistoricalOptionContract.provider == provider,
            HistoricalOptionContract.underlying == underlying.upper(),
        )
        .order_by(HistoricalOptionContract.expiration.desc())
        .first()
    )
    return row[0] if row else None


def latest_stock_bar_date(
    db: Session,
    symbol: str,
    *,
    interval: str,
    provider: str = "massive",
) -> date | None:
    row = (
        db.query(PriceHistory.date_time)
        .filter(
            PriceHistory.provider == provider,
            PriceHistory.symbol == symbol.upper(),
            PriceHistory.interval == interval,
        )
        .order_by(PriceHistory.date_time.desc())
        .first()
    )
    return row[0].date() if row else None


def has_option_bars(db: Session, option_symbol: str, *, interval: str, provider: str = "massive") -> bool:
    return (
        db.query(OptionPriceBar.id)
        .filter(
            OptionPriceBar.provider == provider,
            OptionPriceBar.option_symbol == option_symbol,
            OptionPriceBar.interval == interval,
        )
        .first()
        is not None
    )


def _unique_upper(values: Iterable[str]) -> list[str]:
    return sorted({value.strip().upper() for value in values if value and value.strip()})


def _massive_timespan(interval: str) -> str:
    normalized = interval.lower().strip()
    if normalized in {"1d", "day", "daily"}:
        return "day"
    if normalized in {"1m", "minute"}:
        return "minute"
    if normalized in {"1h", "hour"}:
        return "hour"
    return normalized


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
