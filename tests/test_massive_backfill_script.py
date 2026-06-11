from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.market_data import HistoricalOptionContract
from app.services.massive_backfill import upsert_contracts, upsert_stock_bars
from app.services.massive_client import MassiveBar, MassiveOptionContract
from scripts.massive_backfill import (
    FUTURE_BACKFILL_UNDERLYINGS,
    contract_backfill_end,
    default_backfill_end,
    future_underlyings_for_args,
    primary_backfill_complete,
)


def test_contract_backfill_end_rolls_weekend_to_friday():
    assert contract_backfill_end(date(2026, 5, 31)) == date(2026, 5, 29)
    assert contract_backfill_end(date(2026, 6, 1)) == date(2026, 6, 1)


def test_default_backfill_end_uses_last_completed_weekday():
    assert default_backfill_end(date(2026, 6, 1)) == date(2026, 5, 29)
    assert default_backfill_end(date(2026, 6, 2)) == date(2026, 6, 1)
    assert default_backfill_end(date(2026, 6, 7)) == date(2026, 6, 5)


def test_future_underlyings_default_only_for_primary_queue():
    assert future_underlyings_for_args(args()) == sorted(FUTURE_BACKFILL_UNDERLYINGS)
    assert future_underlyings_for_args(args(underlying=["SPY"])) == []
    assert future_underlyings_for_args(args(future_underlying=["SPY,DIA"])) == ["DIA", "SPY"]
    assert future_underlyings_for_args(args(future_queue=False)) == []


def test_primary_backfill_complete_waits_for_focused_bars_and_contract_coverage():
    session = make_session()
    start = date(2026, 6, 1)
    end = date(2026, 6, 14)
    run_args = args(start=start, end=end)

    upsert_stock_bars(
        session,
        [
            MassiveBar("ASST", datetime(2026, 6, 1), 100, 101, 99, 100),
            MassiveBar("ASST", datetime(2026, 6, 5), 100, 101, 99, 100),
            MassiveBar("IBIT", datetime(2026, 6, 1), 100, 101, 99, 100),
            MassiveBar("IBIT", datetime(2026, 6, 5), 100, 101, 99, 100),
        ],
        interval="1d",
    )
    upsert_contracts(
        session,
        [
            MassiveOptionContract("O:ASST260612C00180000", "ASST", date(2026, 6, 12), "call", 180.0),
            MassiveOptionContract("O:IBIT260612C00110000", "IBIT", date(2026, 6, 12), "call", 110.0),
        ],
    )
    session.commit()

    assert not primary_backfill_complete(session, ["IBIT", "ASST"], run_args)

    ibit_contract = (
        session.query(HistoricalOptionContract)
        .filter(HistoricalOptionContract.provider_symbol == "O:IBIT260612C00110000")
        .one()
    )
    ibit_contract.bars_fetched_interval = "1d"
    ibit_contract.bars_fetched_through = date(2026, 6, 12)
    session.commit()

    assert primary_backfill_complete(session, ["IBIT", "ASST"], run_args)


def args(**overrides):
    values = {
        "as_of": None,
        "dte_lookback_days": 21,
        "end": date(2026, 6, 12),
        "future_queue": True,
        "future_underlying": None,
        "include_active": True,
        "include_expired": True,
        "interval": "1d",
        "max_calls": 50,
        "max_contracts": 50,
        "refresh_existing": False,
        "resume_contracts": True,
        "resume_underlying": True,
        "start": date(2024, 6, 12),
        "underlying": None,
        "underlying_refresh_lookback_days": 7,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()
