"""Realized premium pace: annualize the option premium actually collected per the trade journal.

The dashboard's "annualized premium" and the SATA projection used to be driven entirely by the
*modeled* target roll credit (last recommended trade x 52). That is a pace target, not a track
record. This module instead sums the journal's option credits/debits (sells positive,
buy-to-closes negative, same definition as the Performance page) over trailing windows and
annualizes against the days of journal coverage actually inside each window, so two weeks of
history does not get divided by a 30-day window.

`as_of` anchors at the latest journal trade rather than the wall clock: history arrives in
periodic CSV imports, and letting the pace decay between imports would just add noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.journal import TradeJournalEntry


# Trailing lookbacks (days) shown on the dashboard, shortest first. None = since first trade.
WINDOW_DAYS: tuple[int, ...] = (7, 30, 90)

# Use the trailing 30-day window for the headline projection once there is at least this much
# journal coverage; before that, use everything available.
PROJECTION_WINDOW = 30


@dataclass(frozen=True)
class PremiumWindow:
    label: str
    window_days: int | None  # None = since first journal trade
    net_premium: float  # credits minus debits inside the window
    trade_count: int
    effective_days: int  # days of journal coverage inside the window (annualization basis)
    weekly_rate: float
    annualized: float


@dataclass(frozen=True)
class RealizedPremiumStats:
    windows: list[PremiumWindow]
    projection_weekly: float
    projection_annualized: float
    projection_basis: str
    first_trade_at: datetime | None
    last_trade_at: datetime | None

    @property
    def has_data(self) -> bool:
        return self.last_trade_at is not None


def _empty_stats() -> RealizedPremiumStats:
    return RealizedPremiumStats(
        windows=[],
        projection_weekly=0.0,
        projection_annualized=0.0,
        projection_basis="No option trades in the journal yet.",
        first_trade_at=None,
        last_trade_at=None,
    )


def _window(label: str, window_days: int | None, trades: list[tuple[datetime, float]], as_of: datetime) -> PremiumWindow:
    first_trade = trades[0][0]
    start = first_trade if window_days is None else max(first_trade, as_of - timedelta(days=window_days))
    in_window = [(at, credit) for at, credit in trades if at >= start]
    net = sum(credit for _, credit in in_window)
    effective_days = max((as_of - start).days + 1, 1)
    return PremiumWindow(
        label=label,
        window_days=window_days,
        net_premium=net,
        trade_count=len(in_window),
        effective_days=effective_days,
        weekly_rate=net / effective_days * 7,
        annualized=net / effective_days * 365,
    )


def build_realized_premium_stats(db: Session, as_of: datetime | None = None) -> RealizedPremiumStats:
    """Aggregate realized option premium from the journal into trailing-window paces.

    Returns empty stats (has_data False) when the journal holds no option trades; callers fall
    back to the modeled target pace in that case.
    """
    rows = db.execute(
        select(TradeJournalEntry.created_at, TradeJournalEntry.credit_debit)
        .where(TradeJournalEntry.contracts > 0)
        .order_by(TradeJournalEntry.created_at)
    ).all()
    trades = [(at, float(credit or 0.0)) for at, credit in rows if at is not None]
    if not trades:
        return _empty_stats()

    first_trade_at = trades[0][0]
    last_trade_at = trades[-1][0]
    as_of = as_of or last_trade_at
    span_days = max((as_of - first_trade_at).days + 1, 1)

    windows = [
        _window(f"Last {days} days", days, trades, as_of)
        for days in WINDOW_DAYS
        if days <= max(span_days, WINDOW_DAYS[0])  # skip lookbacks the data cannot fill at all
    ]
    all_time = _window("Since first trade", None, trades, as_of)
    windows.append(all_time)

    if span_days >= PROJECTION_WINDOW:
        basis_window = next(w for w in windows if w.window_days == PROJECTION_WINDOW)
        basis = f"Trailing {PROJECTION_WINDOW}-day realized premium pace ({basis_window.trade_count} trades)."
    else:
        basis_window = all_time
        basis = (
            f"All {span_days} days of journal history ({basis_window.trade_count} trades); "
            f"switches to a trailing {PROJECTION_WINDOW}-day pace once enough history accumulates."
        )

    return RealizedPremiumStats(
        windows=windows,
        projection_weekly=basis_window.weekly_rate,
        projection_annualized=basis_window.annualized,
        projection_basis=basis,
        first_trade_at=first_trade_at,
        last_trade_at=last_trade_at,
    )
