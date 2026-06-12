"""Round-trip option trades and selectable-range return stats from the trade journal.

The journal stores raw broker rows (sell-to-open, buy-to-close, assigned, expired). The
Performance page wants *trades*: each (account, underlying, type, strike, expiration) position
paired from open to close, with the premium taken in, what closing cost, the net P/L, and how the
position ended. Net P/L here is option cash flow only — assignment share economics show up in the
strategy-vs-buy-and-hold comparison, not in the option leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.journal import TradeJournalEntry


def _kind(action: str) -> str | None:
    text = (action or "").upper()
    if "SOLD OPENING" in text:
        return "sell_open"
    if "BOUGHT CLOSING" in text:
        return "buy_close"
    if "BOUGHT OPENING" in text:
        return "buy_open"
    if "SOLD CLOSING" in text:
        return "sell_close"
    if "ASSIGNED" in text:
        return "assigned"
    if "EXPIRED" in text:
        return "expired"
    return None


def _option_type(action: str) -> str:
    return "put" if " PUT " in (action or "").upper() else "call"


@dataclass
class TradeRound:
    account_name: str
    ticker: str
    option_type: str  # call | put
    side: str  # short | long
    strike: float | None
    expiration: object | None
    opened_at: datetime | None
    closed_at: datetime | None  # None while open
    contracts: int
    open_contracts: int  # still open as of as_of
    premium_collected: float  # cash in (sells)
    buyback_cost: float  # cash out (buys), positive number
    net_pnl: float
    outcome: str  # open | expired | assigned | bought back | mixed | opened pre-journal

    @property
    def days_held(self) -> int:
        if not self.opened_at:
            return 0
        end = self.closed_at or datetime.utcnow()
        return max((end - self.opened_at).days, 0)


def build_trade_rounds(db: Session, as_of: datetime | None = None) -> list[TradeRound]:
    rows = (
        db.execute(
            select(TradeJournalEntry)
            .where(TradeJournalEntry.contracts > 0)
            .order_by(TradeJournalEntry.created_at, TradeJournalEntry.id)
        )
        .scalars()
        .all()
    )
    as_of = as_of or datetime.utcnow()

    groups: dict[tuple, list[TradeJournalEntry]] = {}
    for row in rows:
        if _kind(row.action) is None:
            continue
        key = (row.account_name, row.ticker, _option_type(row.action), row.strike, row.expiration)
        groups.setdefault(key, []).append(row)

    rounds = [_build_round(key, entries, as_of) for key, entries in groups.items()]
    rounds.sort(key=lambda r: (r.opened_at or datetime.min), reverse=True)
    return rounds


def _build_round(key: tuple, entries: list[TradeJournalEntry], as_of: datetime) -> TradeRound:
    account_name, ticker, option_type, strike, expiration = key
    opened = closed = 0
    cash_in = cash_out = 0.0
    opened_at: datetime | None = None
    last_close_at: datetime | None = None
    close_kinds: set[str] = set()
    has_long_open = any(_kind(e.action) == "buy_open" for e in entries)
    side = "long" if has_long_open and not any(_kind(e.action) == "sell_open" for e in entries) else "short"

    for entry in entries:
        kind = _kind(entry.action)
        amount = float(entry.credit_debit or 0.0)
        contracts = int(entry.contracts or 0)
        if kind in ("sell_open", "buy_open"):
            opened += contracts
            opened_at = opened_at or entry.created_at
        else:
            closed += contracts
            last_close_at = entry.created_at
            close_kinds.add({"buy_close": "bought back", "sell_close": "sold", "assigned": "assigned", "expired": "expired"}[kind])
        if amount >= 0:
            cash_in += amount
        else:
            cash_out += -amount

    open_contracts = max(opened - closed, 0)
    expired_passively = False
    if open_contracts > 0 and expiration is not None and _as_datetime(expiration) < as_of:
        # No explicit close row, but the contract is past expiration: it expired worthless.
        close_kinds.add("expired")
        last_close_at = _as_datetime(expiration)
        expired_passively = True
        open_contracts = 0

    if open_contracts > 0:
        outcome, closed_at = "open", None
    else:
        closed_at = last_close_at
        if opened == 0:
            outcome = "opened pre-journal"
        elif len(close_kinds) == 1:
            outcome = next(iter(close_kinds))
        elif close_kinds:
            outcome = "mixed (" + ", ".join(sorted(close_kinds)) + ")"
        else:
            outcome = "closed"
    if expired_passively and outcome == "expired" and opened_at and closed_at and closed_at < opened_at:
        closed_at = opened_at

    return TradeRound(
        account_name=account_name or "",
        ticker=ticker,
        option_type=option_type,
        side=side,
        strike=strike,
        expiration=expiration,
        opened_at=opened_at or (entries[0].created_at if entries else None),
        closed_at=closed_at,
        contracts=opened or closed,
        open_contracts=open_contracts,
        premium_collected=cash_in,
        buyback_cost=cash_out,
        net_pnl=cash_in - cash_out,
        outcome=outcome,
    )


@dataclass
class RangeStats:
    start: datetime
    end: datetime
    days: int
    trade_flows: int  # journal option rows in range
    premium_collected: float
    buyback_cost: float
    net_premium: float
    realized_pnl: float  # net P/L of rounds closed inside the range
    closed_rounds: int
    open_rounds: int
    open_premium: float  # premium currently held on open rounds
    annualized_dollars: float
    base_value: float | None  # strategy value near range start, basis for the % figure
    annualized_pct: float | None


def rounds_in_range(rounds: Iterable[TradeRound], start: datetime, end: datetime) -> list[TradeRound]:
    """Rounds that were alive at any point inside [start, end]."""
    end_of_day = end + timedelta(days=1)
    out = []
    for r in rounds:
        opened = r.opened_at or start
        closed = r.closed_at or end_of_day
        if opened < end_of_day and closed >= start:
            out.append(r)
    return out


def summarize_range(
    db: Session,
    rounds: list[TradeRound],
    start: datetime,
    end: datetime,
    base_value: float | None = None,
) -> RangeStats:
    end_of_day = end + timedelta(days=1)
    flows = db.execute(
        select(TradeJournalEntry.credit_debit)
        .where(
            TradeJournalEntry.contracts > 0,
            TradeJournalEntry.created_at >= start,
            TradeJournalEntry.created_at < end_of_day,
        )
    ).scalars().all()
    amounts = [float(a or 0.0) for a in flows]
    premium_collected = sum(a for a in amounts if a > 0)
    buyback_cost = -sum(a for a in amounts if a < 0)
    net_premium = premium_collected - buyback_cost

    in_range = rounds_in_range(rounds, start, end)
    closed = [r for r in in_range if r.closed_at is not None and start <= r.closed_at < end_of_day]
    open_rounds = [r for r in in_range if r.outcome == "open"]

    days = max((end - start).days + 1, 1)
    annualized_dollars = net_premium / days * 365
    annualized_pct = (annualized_dollars / base_value * 100) if base_value else None

    return RangeStats(
        start=start,
        end=end,
        days=days,
        trade_flows=len(amounts),
        premium_collected=premium_collected,
        buyback_cost=buyback_cost,
        net_premium=net_premium,
        realized_pnl=sum(r.net_pnl for r in closed),
        closed_rounds=len(closed),
        open_rounds=len(open_rounds),
        open_premium=sum(r.premium_collected - r.buyback_cost for r in open_rounds),
        annualized_dollars=annualized_dollars,
        base_value=base_value,
        annualized_pct=annualized_pct,
    )


def journal_date_span(db: Session) -> tuple[datetime, datetime] | None:
    row = db.execute(
        select(TradeJournalEntry.created_at).where(TradeJournalEntry.contracts > 0).order_by(TradeJournalEntry.created_at)
    ).scalars().first()
    if row is None:
        return None
    last = db.execute(
        select(TradeJournalEntry.created_at)
        .where(TradeJournalEntry.contracts > 0)
        .order_by(TradeJournalEntry.created_at.desc())
    ).scalars().first()
    return row, last


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, 23, 59)
