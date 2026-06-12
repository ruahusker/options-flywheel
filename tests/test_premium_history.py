from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.journal import TradeJournalEntry
from app.services.premium_history import build_realized_premium_stats


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def add_trade(session, at: datetime, credit: float, contracts: int = 1):
    session.add(
        TradeJournalEntry(
            created_at=at,
            ticker="IBIT",
            strategy="covered call",
            action="YOU SOLD OPENING TRANSACTION",
            contracts=contracts,
            credit_debit=credit,
        )
    )


def test_empty_journal_returns_no_data():
    session = make_session()
    stats = build_realized_premium_stats(session)
    assert not stats.has_data
    assert stats.projection_weekly == 0.0
    assert stats.projection_annualized == 0.0
    assert stats.windows == []


def test_short_history_annualizes_over_actual_span_not_window():
    session = make_session()
    last = datetime(2026, 6, 10)
    # $100/day for 10 days -> $1,000 total over a 10-day span.
    for offset in range(10):
        add_trade(session, last - timedelta(days=offset), 100.0)
    session.commit()

    stats = build_realized_premium_stats(session)
    assert stats.has_data
    all_time = stats.windows[-1]
    assert all_time.window_days is None
    assert all_time.net_premium == 1000.0
    assert all_time.effective_days == 10
    assert all_time.weekly_rate == 1000.0 / 10 * 7
    # Span < 30 days -> projection uses all available history, not a diluted 30-day window.
    assert stats.projection_weekly == all_time.weekly_rate
    assert stats.projection_annualized == 1000.0 / 10 * 365
    # Lookbacks longer than the data span are not shown.
    assert [w.window_days for w in stats.windows] == [7, None]


def test_long_history_uses_trailing_30_day_window():
    session = make_session()
    last = datetime(2026, 6, 10)
    # Old fat premiums (outside 30 days), recent thinner ones (inside).
    add_trade(session, last - timedelta(days=60), 5000.0)
    for offset in (0, 10, 20):
        add_trade(session, last - timedelta(days=offset), 700.0)
    session.commit()

    stats = build_realized_premium_stats(session)
    window_30 = next(w for w in stats.windows if w.window_days == 30)
    assert window_30.net_premium == 2100.0
    assert window_30.trade_count == 3
    assert window_30.effective_days == 31
    assert stats.projection_weekly == window_30.weekly_rate
    assert stats.projection_annualized == 2100.0 / 31 * 365
    # The 60-day-old trade still shows up in the since-first-trade window.
    assert stats.windows[-1].net_premium == 7100.0


def test_buy_to_close_debits_reduce_the_pace():
    session = make_session()
    last = datetime(2026, 6, 10)
    add_trade(session, last - timedelta(days=3), 500.0)
    add_trade(session, last, -200.0)
    session.commit()

    stats = build_realized_premium_stats(session)
    assert stats.windows[-1].net_premium == 300.0
    assert stats.projection_weekly > 0


def test_share_only_journal_rows_are_ignored():
    session = make_session()
    add_trade(session, datetime(2026, 6, 10), 999.0, contracts=0)
    session.commit()
    stats = build_realized_premium_stats(session)
    assert not stats.has_data
